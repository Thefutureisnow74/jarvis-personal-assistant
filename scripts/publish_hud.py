#!/usr/bin/env python3
"""Publish a REDACTED snapshot of the HUD to here.now, so King can look at the
display from his phone.

  publish_hud.py                 # update the saved slug, else create one
  publish_hud.py --new           # force a fresh slug

TWO RULES THIS FILE EXISTS TO ENFORCE:

1. REDACT FIRST. The live HUD carries King's whole day, including a court case
   number and the court's email address. Everything matching REDACTIONS is
   stripped from the brief before the page is rendered, and the rendered bytes are
   then grepped for each secret — a redaction pass that is not verified against the
   OUTPUT has already shipped a case number once.

2. PERSIST THE CLAIM TOKEN. here.now returns claimToken exactly once, at create.
   Without it an anonymous site can be neither updated nor claimed, so the URL
   handed to King dies silently at 24h. It is written to ~/.jarvis/.herenow.json
   immediately, and that file is an ownership credential — never commit it.

The published copy is a SNAPSHOT, not the app: browsers refuse to let an HTTPS
page call http://127.0.0.1, so its input box cannot reach the local server. The
HUD already detects this and says so on the page.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import hud
from common import BRIEF_FILE, STATE_DIR, read_json

API = "https://here.now/api/v1/publish"
TOKEN_FILE = STATE_DIR / ".herenow.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# (pattern, replacement). Anything identifying a legal matter, a person King is in
# dispute with, or a way to contact either.
REDACTIONS = [
    (re.compile(r"\bC-?\d{7,}\b"), "[case no. redacted]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email redacted]"),
    (re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"), "[phone redacted]"),
    (re.compile(r"\b\d+\s+[A-Z][A-Za-z]+\s+"
                r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Dr|Drive|Ln|Lane)\b"),
     "[address redacted]"),
    (re.compile(r"\b(?:Lexington|Municipal|Justice of the Peace)\s+"
                r"(?:Municipal\s+)?Court\b", re.I), "[court redacted]"),
    (re.compile(r"\bcityoflexingtontx\b", re.I), "[redacted]"),
]

# Proven absent from the rendered bytes before the link is handed over.
MUST_NOT_APPEAR = ["C-20083924", "cityoflexingtontx", "@gmail.com",
                   "Lexington Municipal"]


def redact(obj):
    if isinstance(obj, str):
        out = obj
        for pat, rep in REDACTIONS:
            out = pat.sub(rep, out)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    return obj


def build_page() -> str:
    """Redact the brief, render, then redact the RENDERED BYTES as well.

    Both passes are needed. The first-pass-only version was rejected by the guard
    below on its first run: the HUD's capabilities panel prints the live mailbox
    address, which never passes through brief.json at all. Anything the template
    injects from a live probe would leak the same way — so the page itself is the
    last thing scrubbed, and the last thing checked.
    """
    brief = read_json(BRIEF_FILE, default=None)
    if brief is None:
        raise SystemExit("no brief at " + str(BRIEF_FILE))
    page = redact(hud.render(redact(brief)))
    leaked = [s for s in MUST_NOT_APPEAR if s.lower() in page.lower()]
    if leaked:
        raise SystemExit("REFUSING TO PUBLISH — redaction missed: " + ", ".join(leaked))
    return page


def api_key() -> str | None:
    """here.now API key, if King has set one anywhere sensible."""
    from common import load_env_key
    key = load_env_key("HERENOW_API_KEY")
    if key:
        return key
    # The canonical location per here.now's own docs: a bare key, no KEY= prefix.
    try:
        raw = (Path.home() / ".herenow" / "credentials").read_text(
            encoding="utf-8", errors="ignore").strip()
        if raw:
            return raw.splitlines()[0].strip()
    except OSError:
        pass
    for p in (Path.home() / ".config" / "here.now" / ".env",
              Path.home() / ".herenow.env"):
        try:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith("HERENOW_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
        except OSError:
            continue
    return None


def _post(url: str, payload: dict, method: str = "POST") -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    key = api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method=method,
        headers=headers)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8", "ignore") or "{}")


def publish(page: str, slug: str | None, claim: str | None) -> dict:
    body = page.encode("utf-8")
    manifest = {
        "files": [{"path": "index.html", "size": len(body),
                   "contentType": "text/html",
                   "hash": hashlib.sha256(body).hexdigest()}],
        "displayName": "JARVIS HUD",
        "displayDescription": "King Solomon's chief-of-staff display (redacted snapshot)",
    }
    if slug and claim:
        manifest["claimToken"] = claim
        res = _post(f"{API}/{slug}", manifest, "PUT")
    else:
        res = _post(API, manifest)

    up = res.get("upload") or {}
    for entry in up.get("uploads", []):
        req = urllib.request.Request(entry["url"], data=body, method="PUT",
                                     headers={**(entry.get("headers") or {}),
                                              "User-Agent": UA})
        urllib.request.urlopen(req, timeout=120).read()
    if up.get("finalizeUrl"):
        _post(up["finalizeUrl"], {"versionId": up.get("versionId")})
    return res


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--new", action="store_true", help="force a fresh slug")
    a = p.parse_args()

    saved = read_json(TOKEN_FILE, default={}) or {}
    slug = None if a.new else saved.get("slug")
    claim = None if a.new else saved.get("claimToken")

    page = build_page()
    try:
        res = publish(page, slug, claim)
    except urllib.error.HTTPError as exc:
        # NEVER silently create a new site when a slug is already known. Doing so
        # abandons the URL King has been handed — and if he has CLAIMED it, that
        # URL is the permanent one he shares. This exact fallback fired on
        # 2026-08-15 minutes after he claimed civic-breath-mv2h, silently moving
        # his display to a fresh anonymous slug while the claimed URL went stale.
        if slug and exc.code in (401, 403):
            print(
                f"\n  REFUSING to publish elsewhere.\n"
                f"  {slug} is CLAIMED, so here.now now requires an API key —\n"
                f"  the claimToken is no longer accepted (HTTP {exc.code}).\n\n"
                f"  Fix once, permanently:\n"
                f"    1. Get a key at https://here.now  (account -> API keys)\n"
                f"    2. Add  HERENOW_API_KEY=<key>  to ~/.jarvis/.env\n"
                f"    3. Re-run this script — it will update {slug} in place.\n\n"
                f"  Use --new only if you deliberately want a different URL.\n",
                file=sys.stderr)
            return 3
        if slug:
            print(f"[publish] update of {slug} failed ({exc}). Not creating a "
                  f"replacement — re-run with --new if that is what you want.",
                  file=sys.stderr)
            return 3
        raise

    out = {"slug": res.get("slug", slug), "siteUrl": res.get("siteUrl"),
           "claimToken": res.get("claimToken") or claim,
           "claimUrl": res.get("claimUrl") or saved.get("claimUrl")}
    if out["claimToken"]:
        TOKEN_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "claimToken"}, indent=2))
    print(f"\n  live: {out['siteUrl']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
