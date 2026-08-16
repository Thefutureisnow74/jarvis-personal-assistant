#!/usr/bin/env python3
"""Gmail for Jarvis — read-only knowledge of King's inbox.

  mail.py status                     # which mailbox, which token, is it alive
  mail.py recent -n 8                # newest inbox headers
  mail.py search "paint sprayer"     # Gmail's own search, summarised
  mail.py thread <id>                # one message, body included

WHY NOT COMPOSIO: Jarvis is a standalone Python daemon. It cannot call MCP tools,
so routing mail through Composio would mean running a broker process just to reach
a credential King already holds on this machine. The token below is a live Google
OAuth grant for cfmbusiness@gmail.com with gmail.readonly — one refresh call and
we are in. Fewer moving parts, no third party in the path, nothing to re-authorize.

SENDING (added 2026-08-15 on King's order — "the ability to send emails"):
send() exists, but it is reachable ONLY through the two-step spoken gate in
listen.py: Jarvis drafts, reads recipient + subject + gist ALOUD, and nothing
leaves until King says "send it". A voice assistant that can be triggered by
something it misheard must never be one utterance away from mailing a stranger
— the read-back is the seatbelt, not a formality. Nothing here deletes,
archives or modifies existing mail.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

from common import STATE_DIR
from config import cfg_paths

# Tokens on this machine, best first. Each is checked for a Gmail scope and a
# refresh token, then PROVEN by an actual refresh (see credentials()).
#
# Jarvis's own copy leads deliberately. Depending on another agent's token file
# means Jarvis goes dark the day that folder is moved, rotated or cleaned — a
# failure that would look like "Gmail is broken" and send someone hunting
# through this module instead of the filesystem. Same OAuth client and refresh
# token, so both copies stay valid independently; Google issues access tokens per
# request, not per file.
#
# Extra candidates (other agents' tokens on THIS machine, per-machine) come from
# config.py — on King's own machine that still includes the revoked .mer copy,
# kept only so a future session sees it tried and failing rather than
# rediscovering it and assuming it should work. A fresh install has none.
TOKEN_CANDIDATES = [Path.home() / ".jarvis" / "google_token.json",
                    *cfg_paths("extra_google_tokens")]

READ_SCOPES = ("gmail.readonly", "gmail.modify", "mail.google.com")
CACHE = STATE_DIR / "mail_cache.json"


def _scopes_of(d: dict) -> list[str]:
    s = d.get("scopes") or d.get("scope") or []
    return s.split() if isinstance(s, str) else list(s)


def _candidates() -> list[tuple[Path, dict]]:
    """Every stored grant that *claims* a Gmail scope, best first.

    Returns a list, not the first hit. Google tokens on this machine expire and
    get revoked independently, and stopping at the first one that merely looks
    right meant a dead token masked three live ones. A candidate is only proven
    by a successful refresh, so the caller must be free to keep trying.
    """
    out = []
    for p in TOKEN_CANDIDATES:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # google-workspace-mcp nests the grant one level down, keyed by address.
        nested = [d] if ("refresh_token" in d or "token" in d) else list(d.values())
        for cand in nested:
            if not isinstance(cand, dict) or not cand.get("refresh_token"):
                continue
            if any(any(r in s for r in READ_SCOPES) for s in _scopes_of(cand)):
                out.append((p, cand))
    return out


def _try_credentials(path: Path, raw: dict):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    try:
        creds = Credentials.from_authorized_user_info(raw)
    except Exception as exc:
        return None, f"unusable: {exc}"
    if creds.valid:
        return creds, None
    if not creds.refresh_token:
        return None, "no refresh token"
    try:
        creds.refresh(Request())
    except Exception as exc:
        return None, str(exc).split(",")[0].strip("(' ")
    try:                       # persist, but a read-only file must not break us
        stored = json.loads(path.read_text(encoding="utf-8"))
        if "refresh_token" in stored:
            path.write_text(creds.to_json(), encoding="utf-8")
    except Exception:
        pass
    return creds, None


def credentials(explain: bool = False):
    """A refreshed Credentials object from the first token that actually works."""
    errors = []
    for path, raw in _candidates():
        creds, err = _try_credentials(path, raw)
        if creds:
            if explain:
                print(f"[mail] using {path}", file=sys.stderr)
            return creds
        errors.append(f"{path.name}: {err}")
    if errors:
        print("[mail] no working Google token — " + " | ".join(errors), file=sys.stderr)
    else:
        print("[mail] no Google token with a Gmail scope found", file=sys.stderr)
    return None


def which_token() -> str | None:
    for path, raw in _candidates():
        creds, _ = _try_credentials(path, raw)
        if creds:
            return str(path)
    return None


def service():
    creds = credentials()
    if not creds:
        return None
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ------------------------------------------------------------------ reading

def _headers(msg: dict) -> dict:
    return {h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])}


def _body_text(payload: dict, limit: int = 4000) -> str:
    """Depth-first walk for the first text/plain part, falling back to stripped
    HTML. Gmail nests multipart/alternative inside multipart/mixed inside... —
    a single-level check misses the body on most real mail."""
    stack, html = [payload], ""
    while stack:
        part = stack.pop(0)
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            try:
                text = base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
            except Exception:
                text = ""
            if mime == "text/plain" and text.strip():
                return text[:limit]
            if mime == "text/html" and not html:
                html = text
        stack.extend(part.get("parts", []) or [])
    if html:
        html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        return re.sub(r"\s{2,}", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()[:limit]
    return ""


def summarize(svc, ids: list[str], with_body: bool = False) -> list[dict]:
    out = []
    for mid in ids:
        fmt = "full" if with_body else "metadata"
        kw = {} if with_body else {"metadataHeaders": ["From", "Subject", "Date"]}
        try:
            m = svc.users().messages().get(userId="me", id=mid, format=fmt, **kw).execute()
        except Exception as exc:
            print(f"[mail] get {mid}: {exc}", file=sys.stderr)
            continue
        h = _headers(m)
        item = {"id": mid, "from": h.get("from", ""), "subject": h.get("subject", ""),
                "date": h.get("date", ""), "snippet": (m.get("snippet") or "").strip(),
                "unread": "UNREAD" in (m.get("labelIds") or [])}
        if with_body:
            item["body"] = _body_text(m.get("payload", {}))
        out.append(item)
    return out


def search(query: str, k: int = 6, with_body: bool = False) -> list[dict]:
    """Gmail's own search operators work here — 'from:', 'newer_than:', 'has:'.
    Gmail indexes the mailbox far better than we could locally, so there is no
    local mail index to go stale."""
    svc = service()
    if not svc:
        return []
    try:
        r = svc.users().messages().list(userId="me", q=query, maxResults=k).execute()
    except Exception as exc:
        print(f"[mail] search failed: {exc}", file=sys.stderr)
        return []
    return summarize(svc, [m["id"] for m in r.get("messages", [])], with_body)


def query_from_question(question: str) -> str:
    """Spoken English -> a Gmail search string.

    Gmail ANDs bare terms, which makes a natural question ("did the paint sprayer
    people ever get back to me") match nothing. ORing the content words matches
    broadly and lets Gmail's own relevance ranking do the choosing.
    """
    import brain
    words = [w.lower() for w in brain.WORD.findall(question)]
    words = [w for w in words if w not in brain.STOP and len(w) > 2]
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    if not out:
        return "in:inbox newer_than:7d"
    return "(" + " OR ".join(out[:12]) + ")"


def recent(n: int = 8, unread_only: bool = False) -> list[dict]:
    q = "in:inbox" + (" is:unread" if unread_only else "")
    return search(q, k=n)


# ------------------------------------------------------------------ sending

def resolve_address(name: str) -> tuple[str | None, list[str]]:
    """(best_address, all_candidates) for a spoken name, from the mailbox itself.

    The mailbox is the contact book King actually maintains: whoever he mails
    and receives mail from is in there, with the exact address. Searching it is
    read-only, needs no extra scope, and cannot invent an address — the failure
    mode is 'no candidate', never 'wrong stranger'.
    """
    if "@" in name:
        return name.strip(), [name.strip()]
    svc = service()
    if not svc:
        return None, []
    counts: dict[str, int] = {}
    for q in (f"from:{name}", f"to:{name}"):
        try:
            r = svc.users().messages().list(userId="me", q=q, maxResults=12).execute()
            for m in summarize(svc, [x["id"] for x in r.get("messages", [])]):
                for field in (m.get("from", ""),):
                    am = re.search(r"<([^>]+@[^>]+)>", field) or \
                         re.search(r"([\w.+-]+@[\w.-]+\.\w+)", field)
                    if am:
                        addr = am.group(1).lower()
                        if name.lower().split()[0] in field.lower() or \
                           name.lower().split()[0] in addr:
                            counts[addr] = counts.get(addr, 0) + 1
        except Exception as exc:
            print(f"[mail] resolve {q}: {exc}", file=sys.stderr)
    ranked = sorted(counts, key=counts.get, reverse=True)
    return (ranked[0] if ranked else None), ranked


def send(to: str, subject: str, body: str) -> dict:
    """Send one plain-text email as King. Callers own the confirm gate."""
    svc = service()
    if not svc:
        return {"ok": False, "error": "no working Google token"}
    if not re.match(r"^[\w.+-]+@[\w.-]+\.\w+$", (to or "").strip()):
        return {"ok": False, "error": f"not a valid address: {to!r}"}
    from email.mime.text import MIMEText
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to.strip()
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        r = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"ok": True, "id": r.get("id"), "to": to.strip(), "subject": subject}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def status() -> dict:
    cands = _candidates()
    if not cands:
        return {"ok": False, "error": "no Google token with a Gmail scope"}
    svc = service()
    if not svc:
        return {"ok": False, "checked": [str(p) for p, _ in cands],
                "error": "no stored Google token would refresh — "
                         "re-authorize with: python scripts/reauth_google.py"}
    path = which_token()
    try:
        p = svc.users().getProfile(userId="me").execute()
    except Exception as exc:
        return {"ok": False, "token": str(path), "error": str(exc)[:200]}
    try:
        un = svc.users().labels().get(userId="me", id="UNREAD").execute()
        unread = un.get("messagesUnread") or un.get("threadsUnread") or 0
    except Exception:
        unread = None
    return {"ok": True, "mailbox": p.get("emailAddress"),
            "messages_total": p.get("messagesTotal"), "unread": unread,
            "token": str(path), "access": "read + send (spoken confirm gate)"}

# Markers that only appear when UTF-8 was decoded as cp1252/latin-1: the Ã/Â
# lead bytes, the 'â€' trigraph prefix, and the C1 control block. It must NOT
# include — “ ” ’ — those are what a SUCCESSFUL repair produces, and counting
# them as damage made the loop reject its own fix one round short of done:
# 'Ã¢Â€Â”' repaired to 'â' and then refused the final step to '—'.
_MOJI = re.compile("[ÃÂ]|â€|[-]")


def demojibake(s: str, rounds: int = 4) -> str:
    """Repair text that was UTF-8 encoded and then decoded as cp1252/latin-1.

    Found live 2026-08-15 in King's own mail: an "Ivy morning briefing" subject
    carried a DOUBLE-mojibaked em-dash, so one repair pass is not enough. Spoken
    aloud that is pure noise, and it also poisons the retrieval index.

    Both codecs are tried each round, and that is not belt-and-braces. cp1252
    undoes the first layer, but the intermediate it produces contains C1 control
    characters (U+0080-U+009F) that cp1252 CANNOT encode at all — only latin-1
    maps them straight through. A cp1252-only loop raises UnicodeEncodeError on
    round two and silently returns the still-broken string.

    Repairs only while the result gets cleaner, so ordinary accented text
    (Jose, cafe, Zurich) is never mangled by an over-eager round trip.
    """
    out = s
    for _ in range(rounds):
        before = len(_MOJI.findall(out))
        if not before:
            break
        best = None
        for codec in ("cp1252", "latin-1"):
            try:
                cand = out.encode(codec, "strict").decode("utf-8", "strict")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            score = len(_MOJI.findall(cand))
            if score < before and (best is None or score < best[1]):
                best = (cand, score)
        if best is None:
            break
        out = best[0]
    return out


def line(m: dict, max_subject: int = 72) -> str:
    """One spoken-friendly line.

    Strips the angle-bracket address — read aloud, 'King Solomon <cfm at gmail dot
    com>' is noise — and truncates the subject, because calendar and newsletter
    subjects run to 120 characters and five of them is a minute of monologue.
    """
    who = re.sub(r"\s*<[^>]*>", "", m.get("from", "")).strip().strip('"') or "unknown"
    if "@" in who and who.count("@") == 1 and " " not in who:
        who = who.split("@")[0]          # a bare address reads better as the handle
    who = demojibake(who)
    subj = demojibake((m.get("subject") or "(no subject)").strip())
    subj = re.sub(r"\s+", " ", subj)
    if len(subj) > max_subject:
        subj = subj[:max_subject].rsplit(" ", 1)[0] + "…"
    return f"{who}: {subj}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    pr = sub.add_parser("recent"); pr.add_argument("-n", type=int, default=8)
    pr.add_argument("--unread", action="store_true")
    pse = sub.add_parser("search"); pse.add_argument("q")
    pse.add_argument("-k", type=int, default=6); pse.add_argument("--body", action="store_true")
    pt = sub.add_parser("thread"); pt.add_argument("id")
    pd = sub.add_parser("send")
    pd.add_argument("--to", required=True); pd.add_argument("--subject", required=True)
    pd.add_argument("--body", required=True)
    pv = sub.add_parser("resolve"); pv.add_argument("name")
    a = p.parse_args()

    if a.cmd == "send":
        print(json.dumps(send(a.to, a.subject, a.body), indent=2))
        return 0
    if a.cmd == "resolve":
        best, cands = resolve_address(a.name)
        print(json.dumps({"best": best, "candidates": cands}, indent=2))
        return 0

    if a.cmd == "status":
        print(json.dumps(status(), indent=2))
    elif a.cmd == "recent":
        print(json.dumps(recent(a.n, a.unread), indent=2, ensure_ascii=False))
    elif a.cmd == "search":
        print(json.dumps(search(a.q, a.k, a.body), indent=2, ensure_ascii=False))
    else:
        svc = service()
        print(json.dumps(summarize(svc, [a.id], True) if svc else [],
                         indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
