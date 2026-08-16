#!/usr/bin/env python3
"""The second brain Jarvis actually searches — ObsiVault + King's computer files.

One SQLite FTS5 index at ~/.jarvis/brain.db covering three corpora:

  memory every Claude Code auto-memory file about King specifically —
         birthdate, hard rules, preferences, project state
  vault  every page of C:\\Users\\12149\\Documents\\Hermes Agents\\ObsiVault
  file   every file under Documents / Desktop / Downloads (content where it is
         text, name-and-path only where it is not)

memory and vault are curated (185 + 113 files); file is the whole disk
(~23,000). Left blended in one bm25 ranking, file always won on term
statistics and the curated corpora never reached the model — so retrieval
draws a fixed quota from each corpus instead of ranking them against each
other. See context_for().

FTS5 was chosen over an in-memory index because the file corpus is ~23,000
entries: a JSON index would be tens of megabytes reloaded on every question,
where SQLite answers in single-digit milliseconds and updates incrementally.
bm25() is built in, so ranking is the engine's job, not ours.

  brain.py index                 # incremental — only changed files are re-read
  brain.py index --full          # rebuild from scratch
  brain.py search "paint sprayer"
  brain.py search "retell" --source vault
  brain.py status

SECRETS ARE NEVER INDEXED BY CONTENT. Anything matching SECRET_NAMES gets a
name-only row, so "where is my .env" still answers while the key inside it can
never reach an LLM prompt. Adding a content path for those files would leak
credentials into every answer Jarvis speaks — do not do it.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from common import STATE_DIR, ensure_state_dir

DB = STATE_DIR / "brain.db"

from config import cfg_path, cfg_paths          # per-machine paths, never hardcoded

VAULT = cfg_path("vault_dir")
VAULT_SKIP = {"raw", ".git", ".obsidian", "templates"}

MEMORY_DIR = (cfg_paths("memory_dirs") or [Path()])[0]

# Jarvis's OWN memory — Canon-in-spirit (2026-08-16, King: "I want Jarvis to
# have its own memory like the hermes agents use Canon"). One durable fact per
# markdown file, written by Jarvis himself: explicitly ("remember that...") or
# auto-captured from conversation. Not the VPS Canon codebase — a second
# database was the wrong trade when a proven FTS5 engine already ranks four
# corpora; what makes Canon canon is curation and durability, and files give
# both, plus King can read and edit his assistant's memory in Notepad.
JMEM_DIR = STATE_DIR / "memories"

FILE_ROOTS = [Path.home() / "Documents", Path.home() / "Desktop",
              Path.home() / "Downloads"]

# Directories that are machine output, not King's work. Indexing them buries every
# real answer under dependency source.
DIR_SKIP = {"node_modules", ".git", "venv", ".venv", "env", "__pycache__",
            "site-packages", "dist", "build", ".next", "AppData", ".cache",
            ".tmp", "Lib", ".pytest_cache", ".mypy_cache", "vendor",
            "bower_components", ".gradle", "target", "obj", "bin"}

TEXT_EXT = {".md", ".txt", ".rtf", ".csv", ".tsv", ".json", ".yaml", ".yml",
            ".html", ".htm", ".xml", ".py", ".js", ".ts", ".tsx", ".jsx",
            ".sh", ".ps1", ".psm1", ".sql", ".ini", ".cfg", ".toml", ".bat",
            ".cmd", ".r", ".rb", ".go", ".java", ".c", ".h", ".cpp", ".cs",
            ".css", ".scss", ".vue", ".svelte", ".log", ".srt", ".vtt"}

# Content from these never enters the index. Name-only rows still get written.
SECRET_NAMES = [".env", ".env.*", "*.env", "*token*.json", "*credential*",
                "*secret*", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*",
                "*.keystore", "auth.json", "*apikey*", "*api_key*"]

MAX_CONTENT = 20_000        # chars indexed per file — enough to answer from
MAX_FILE_BYTES = 4_000_000  # above this a "text" file is a data dump, not a doc

WORD = re.compile(r"[A-Za-z0-9_']+")
STOP = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "at",
    "for", "with", "is", "are", "was", "were", "be", "been", "am", "do", "does",
    "did", "have", "has", "had", "i", "me", "my", "you", "your", "it", "its",
    "that", "this", "these", "those", "what", "which", "who", "whom", "when",
    "where", "why", "how", "can", "could", "would", "should", "will", "shall",
    "about", "from", "as", "by", "so", "than", "then", "there", "here", "any",
    "all", "some", "just", "tell", "know", "know", "find", "show", "get", "give",
    "please", "jarvis", "sir", "s", "t", "re", "ve", "ll", "d", "m",
}


# ------------------------------------------------------------------ storage

def connect() -> sqlite3.Connection:
    ensure_state_dir()
    c = sqlite3.connect(str(DB))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
                   title, path, body,
                   source UNINDEXED, mtime UNINDEXED, size UNINDEXED,
                   tokenize='unicode61 remove_diacritics 2')""")
    # FTS5 cannot be indexed on an UNINDEXED column, so incremental reindex needs
    # its own key->mtime table. Without it every run re-read all 23,000 files.
    c.execute("""CREATE TABLE IF NOT EXISTS seen(
                   key TEXT PRIMARY KEY, mtime REAL, rowid_ref INTEGER)""")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return c


def _meta_set(c: sqlite3.Connection, k: str, v) -> None:
    c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v)))


def _meta_get(c: sqlite3.Connection, k: str, default=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


# ------------------------------------------------------------------ helpers

def is_secret(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat) for pat in SECRET_NAMES)


# Documents whose text is real but not sitting in plain bytes. Without these, 791
# PDFs and every Word/Excel file on the machine were findable by NAME only — King
# could not ask what was *in* his own paperwork.
DOC_EXT = {".pdf", ".docx", ".xlsx", ".pptx"}
MAX_DOC_BYTES = 25_000_000     # a PDF past this is a scan, and scans have no text
PDF_PAGES = 15                 # front matter carries the subject; 400-page appendices do not

_XML_TAG = re.compile(r"<[^>]+>")


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    if getattr(reader, "is_encrypted", False):
        return ""
    out = []
    for page in reader.pages[:PDF_PAGES]:
        try:
            out.append(page.extract_text() or "")
        except Exception:
            continue
        if sum(len(x) for x in out) > MAX_CONTENT:
            break
    return "\n".join(out)


def _ooxml_text(path: Path, members: tuple[str, ...]) -> str:
    """Word/PowerPoint text without a third-party dependency.

    An OOXML file is a zip of XML; stripping the tags out of the document part
    yields the prose. python-docx is not installed on this machine and adding a
    dependency to read a zip would be the wrong trade.
    """
    import zipfile
    out = []
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist()
                 if any(n.startswith(m) for m in members) and n.endswith(".xml")]
        for n in sorted(names)[:60]:
            try:
                xml = z.read(n).decode("utf-8", "ignore")
            except Exception:
                continue
            # Close each paragraph/cell with a space or every word runs together.
            xml = re.sub(r"</(w:p|a:p|w:tc)>", " ", xml)
            out.append(_XML_TAG.sub("", xml))
            if sum(len(x) for x in out) > MAX_CONTENT:
                break
    return re.sub(r"\s{2,}", " ", " ".join(out)).strip()


def _xlsx_text(path: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets[:8]:
            out.append(str(ws.title))
            for row in ws.iter_rows(max_rows=120, values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    out.append(" ".join(cells))
                if sum(len(x) for x in out) > MAX_CONTENT:
                    return "\n".join(out)
    finally:
        wb.close()
    return "\n".join(out)


def extract_doc(path: Path) -> str:
    """Text out of a PDF or Office file. Never raises — an unreadable document
    degrades to a name-only row, exactly as a .jpg does."""
    ext = path.suffix.lower()
    try:
        if path.stat().st_size > MAX_DOC_BYTES:
            return ""
        if ext == ".pdf":
            return _pdf_text(path)[:MAX_CONTENT]
        if ext == ".docx":
            return _ooxml_text(path, ("word/document", "word/footnotes"))[:MAX_CONTENT]
        if ext == ".pptx":
            return _ooxml_text(path, ("ppt/slides/slide",))[:MAX_CONTENT]
        if ext == ".xlsx":
            return _xlsx_text(path)[:MAX_CONTENT]
    except Exception:
        return ""
    return ""


def read_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")[:MAX_CONTENT]
    except OSError:
        return ""


def _upsert(c: sqlite3.Connection, key: str, mtime: float, title: str,
            path: str, body: str, source: str, size: int) -> None:
    """Replace this document's row, keeping `seen` and `docs` in step.

    The old row is found via `seen` and, failing that, by (path, source). That
    second lookup is not redundant: forcing a re-extract by deleting `seen` rows —
    the documented way to re-read PDFs after changing the extractor — severs the
    pointer, and a seen-only delete then INSERTS a second copy instead of
    replacing. That silently duplicated 761 documents once; two rows for one file
    means the same passage is shown to the model twice.
    """
    row = c.execute("SELECT rowid_ref FROM seen WHERE key=?", (key,)).fetchone()
    if row:
        c.execute("DELETE FROM docs WHERE rowid=?", (row[0],))
    else:
        c.execute("DELETE FROM docs WHERE path=? AND source=?", (path, source))
    cur = c.execute(
        "INSERT INTO docs(title,path,body,source,mtime,size) VALUES(?,?,?,?,?,?)",
        (title, path, body, source, mtime, size))
    c.execute("INSERT OR REPLACE INTO seen VALUES(?,?,?)",
              (key, mtime, cur.lastrowid))


# ------------------------------------------------------------------ indexers

def index_vault(c: sqlite3.Connection, full: bool = False) -> dict:
    if not VAULT.exists():
        return {"indexed": 0, "error": f"vault not found: {VAULT}"}
    n = skipped = 0
    for f in VAULT.rglob("*.md"):
        if any(p in VAULT_SKIP for p in f.relative_to(VAULT).parts):
            continue
        key = "vault:" + str(f)
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if not full:
            prev = c.execute("SELECT mtime FROM seen WHERE key=?", (key,)).fetchone()
            if prev and abs(prev[0] - mt) < 0.5:
                skipped += 1
                continue
        body = read_text(f)
        # The wiki path is itself meaningful ("wiki/entities/Ivy.md" says Ivy is an
        # entity), so it goes in the searchable path column, not just as a pointer.
        rel = str(f.relative_to(VAULT)).replace("\\", "/")
        _upsert(c, key, mt, f.stem.replace("-", " "), rel, body, "vault",
                len(body))
        n += 1
    return {"indexed": n, "unchanged": skipped}


def index_memory(c: sqlite3.Connection, full: bool = False) -> dict:
    """185 curated files about King specifically — birthdate, hard rules,
    preferences, project state. Same shape as index_vault(); source='memory'.

    This is the corpus "when is my birthday" actually lives in — the vault
    barely mentions it. Without this, personal facts King told a Claude Code
    session were invisible to Jarvis no matter how good retrieval got."""
    if not MEMORY_DIR.exists():
        return {"indexed": 0, "error": f"memory dir not found: {MEMORY_DIR}"}
    n = skipped = 0
    for f in MEMORY_DIR.rglob("*.md"):
        if is_secret(f.name):
            continue
        key = "memory:" + str(f)
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if not full:
            prev = c.execute("SELECT mtime FROM seen WHERE key=?", (key,)).fetchone()
            if prev and abs(prev[0] - mt) < 0.5:
                skipped += 1
                continue
        body = read_text(f)
        rel = str(f.relative_to(MEMORY_DIR)).replace("\\", "/")
        _upsert(c, key, mt, f.stem.replace("-", " "), rel, body, "memory", len(body))
        n += 1
    return {"indexed": n, "unchanged": skipped}


def index_jmem(c: sqlite3.Connection, full: bool = False) -> dict:
    """Jarvis's own memories — what King told HIM, in his own store.

    Also PRUNES: deleting a file from ~/.jarvis/memories IS forgetting, so a
    row whose file is gone must leave the index too — otherwise a 'deleted'
    memory keeps speaking in answers forever, which is worse than remembering
    wrong."""
    if not JMEM_DIR.exists():
        return {"indexed": 0}
    for (key,) in c.execute(
            "SELECT key FROM seen WHERE key LIKE 'jarvis:%'").fetchall():
        if not Path(key[len("jarvis:"):]).exists():
            row = c.execute("SELECT rowid_ref FROM seen WHERE key=?", (key,)).fetchone()
            if row:
                c.execute("DELETE FROM docs WHERE rowid=?", (row[0],))
            c.execute("DELETE FROM seen WHERE key=?", (key,))
    n = skipped = 0
    for f in JMEM_DIR.glob("*.md"):
        key = "jarvis:" + str(f)
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if not full:
            prev = c.execute("SELECT mtime FROM seen WHERE key=?", (key,)).fetchone()
            if prev and abs(prev[0] - mt) < 0.5:
                skipped += 1
                continue
        body = read_text(f)
        _upsert(c, key, mt, f.stem.replace("-", " "), f.stem + ".md", body,
                "jarvis", len(body))
        n += 1
    return {"indexed": n, "unchanged": skipped}


def remember(fact: str, slug: str) -> Path:
    """Write one durable fact into Jarvis's own memory and index it NOW —
    'remember X' followed ten seconds later by 'what did I say about X' must
    work, so indexing cannot wait for the next rescan tick."""
    JMEM_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9-]", "", slug.lower().replace(" ", "-"))[:60] or "memory"
    path = JMEM_DIR / f"{slug}.md"
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if path.exists():   # same topic again: append, never clobber what he said before
        path.write_text(path.read_text(encoding="utf-8").rstrip()
                        + f"\n\nUpdate ({stamp}): {fact}\n", encoding="utf-8")
    else:
        path.write_text(f"# {slug.replace('-', ' ')}\n\n{fact}\n\n"
                        f"(King told Jarvis this on {stamp})\n", encoding="utf-8")
    c = connect()
    index_jmem(c)
    c.commit()
    c.close()
    return path


def index_files(c: sqlite3.Connection, full: bool = False,
                roots: list[Path] | None = None, progress: bool = False) -> dict:
    roots = roots or FILE_ROOTS
    n = skipped = nameonly = secrets = docs = 0
    t0 = time.time()
    # The ObsiVault sits under Documents, so a plain walk indexed every wiki page
    # twice — once as source 'vault', once as source 'file'. Retrieval then spent
    # half its budget showing the model the same page twice.
    vault_real = str(VAULT.resolve()).lower()
    for root in roots:
        if not root.exists():
            continue
        for dp, dn, fn in os.walk(root):
            if str(Path(dp).resolve()).lower().startswith(vault_real):
                dn[:] = []
                continue
            dn[:] = [d for d in dn
                     if d not in DIR_SKIP and not d.startswith(".")]
            for name in fn:
                p = Path(dp) / name
                key = "file:" + str(p)
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not full:
                    prev = c.execute("SELECT mtime FROM seen WHERE key=?",
                                     (key,)).fetchone()
                    if prev and abs(prev[0] - st.st_mtime) < 0.5:
                        skipped += 1
                        continue
                ext = p.suffix.lower()
                if is_secret(name):
                    body, secrets = "", secrets + 1
                elif ext in TEXT_EXT:
                    body = read_text(p)
                elif ext in DOC_EXT:
                    body = extract_doc(p)
                    if body:
                        docs += 1
                    else:
                        nameonly += 1       # encrypted, scanned, or malformed
                else:
                    body, nameonly = "", nameonly + 1
                # The folder path carries most of the meaning for a name-only file
                # ("Solomon's Trading System/backtests/..."), so it is searchable.
                rel = str(p).replace("\\", "/")
                _upsert(c, key, st.st_mtime, p.stem.replace("_", " ").replace("-", " "),
                        rel, body, "file", st.st_size)
                n += 1
                if progress and n % 2000 == 0:
                    print(f"  .. {n} indexed ({time.time()-t0:.0f}s)", flush=True)
        c.commit()
    return {"indexed": n, "unchanged": skipped, "name_only": nameonly,
            "docs_extracted": docs, "secrets_name_only": secrets}


def reindex(full: bool = False, progress: bool = False) -> dict:
    c = connect()
    if full:
        c.execute("DELETE FROM docs")
        c.execute("DELETE FROM seen")
        c.commit()
    t0 = time.time()
    j = index_jmem(c, full)
    c.commit()
    m = index_memory(c, full)
    c.commit()
    v = index_vault(c, full)
    c.commit()
    f = index_files(c, full, progress=progress)
    _meta_set(c, "last_index", time.time())
    c.commit()
    total = c.execute("SELECT count(*) FROM docs").fetchone()[0]
    c.close()
    return {"ok": True, "jarvis": j, "memory": m, "vault": v, "files": f,
            "rows": total, "seconds": round(time.time() - t0, 1)}


# ------------------------------------------------------------------ search

def match_query(question: str) -> str:
    """Turn spoken English into a safe FTS5 MATCH expression.

    Every token is quoted, so FTS5 operators a question happens to contain
    ("OR", "NEAR", "*", ":", "-") are treated as literal words instead of syntax.
    An unquoted question is not merely wrong, it raises OperationalError and the
    whole answer path dies — this function is the only place queries are built.
    """
    toks = [w.lower() for w in WORD.findall(question)]
    toks = [w for w in toks if w not in STOP and len(w) > 1]
    seen, out = set(), []
    for w in toks:
        if w not in seen:
            seen.add(w)
            out.append('"' + w.replace('"', '') + '"')
    return " OR ".join(out[:24])


def search(question: str, source: str | None = None, k: int = 6) -> list[dict]:
    """Top-k passages. Empty list when nothing matches — never a guess."""
    expr = match_query(question)
    if not expr or not DB.exists():
        return []
    c = connect()
    # Weights: title 8, path 4, body 1. A file literally named for the thing King
    # asked about should outrank one that merely mentions it in passing.
    sql = ("SELECT title, path, source, size, "
           "       snippet(docs, 2, '', '', ' … ', 24) AS snip, "
           "       bm25(docs, 8.0, 4.0, 1.0) AS rank "
           "FROM docs WHERE docs MATCH ? ")
    args: list = [expr]
    if source:
        sql += "AND source = ? "
        args.append(source)
    sql += "ORDER BY rank LIMIT ?"
    args.append(k)
    try:
        rows = c.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()
    return [{"title": r[0], "path": r[1], "source": r[2], "size": r[3],
             "snippet": (r[4] or "").strip(), "rank": r[5]} for r in rows]


def passage(hit: dict, chars: int = 2500) -> str:
    """Full text for a hit, read live from disk so it is never stale."""
    p = hit["path"]
    path = (VAULT / p) if hit["source"] == "vault" else \
           (JMEM_DIR / p) if hit["source"] == "jarvis" else Path(p)
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:chars]
    except OSError:
        return hit.get("snippet", "")


# ------------------------------------------------------------------ recall

# Which corpora a question needs. Gmail is a network round trip (~1s), so it is
# only consulted when the question smells like mail or when nothing local matched
# — otherwise every "what's my top three" would wait on Google.
MAIL_HINT = re.compile(
    r"\b(e-?mails?|inbox|gmail|mailbox|unread|message[sd]?|wrote|written|"
    r"sent|send|reply|replied|forwarded|newsletter|receipt|invoice|"
    r"subscription|notification)\b", re.I)


# Curated corpora first, disk last. A blended ranking always lost the curated
# pages to the 22,827-file disk corpus on term statistics alone (0/5 vault hits
# across 8 live tests, 2026-08-15) — so each corpus gets its own guaranteed
# slots instead of competing for one shared top-k. Do NOT "fix" this by raising
# the title weight in search(); it is already 8.0 and that is *why* bare
# filename matches beat curated pages in the first place.
QUOTA = (("jarvis", 3), ("memory", 3), ("vault", 3), ("file", 3))

# A name-only row (a .mp4, a Start Menu shortcut, a binary) matches on filename
# and carries no prose — feeding it to the model gave it fragments to CONTINUE
# rather than facts to answer from (the 2026-08-15 "MiniMax" bleed). memory and
# vault files are short by nature (a birthdate is one line), so their gate is
# lower than file's — a terse-but-real memory must not be dropped for brevity.
PROSE_GATE = {"jarvis": 20, "memory": 60, "vault": 60, "file": 120}
TAG = {"jarvis": "JARVIS MEMORY", "memory": "MEMORY",
       "vault": "VAULT PAGE", "file": "FILE"}


def context_for(question: str, k_mail: int = 4,
                chars: int = 1100) -> tuple[str, list[dict]]:
    """Assemble grounding for a spoken question from every corpus Jarvis owns.

    Returns (context_block, sources). An empty block is a legitimate answer — it
    means nothing was found, and the caller must say so rather than let the model
    fill the silence from its own weights.
    """
    blocks, sources = [], []
    any_local = False

    for src, n in QUOTA:
        hits = search(question, source=src, k=n * 2)
        any_local = any_local or bool(hits)
        gate = PROSE_GATE[src]
        substantive = [(h, t) for h in hits
                       if len(t := passage(h, chars).strip()) >= gate]
        if not substantive:                  # nothing prose-like: fall back to snippets
            substantive = [(h, h["snippet"]) for h in hits[:n] if h["snippet"]]
        for h, text in substantive[:n]:
            blocks.append(f"[{TAG[src]}: {h['title']} — {h['path']}]\n{text}")
            sources.append({"kind": h["source"], "title": h["title"], "path": h["path"]})

    if MAIL_HINT.search(question) or not any_local:
        try:
            import mail
            for m in mail.search(mail.query_from_question(question), k=k_mail):
                blocks.append(
                    f"[EMAIL from {m['from']} — {m['subject']} — {m['date']}]\n"
                    f"{m['snippet']}")
                sources.append({"kind": "email", "title": m["subject"],
                                "path": m["from"]})
        except Exception as exc:
            print(f"[brain] mail lookup failed: {exc}", file=sys.stderr)

    return "\n\n".join(blocks)[:9000], sources


def status() -> dict:
    if not DB.exists():
        return {"indexed": False, "hint": "run: brain.py index"}
    c = connect()
    rows = c.execute("SELECT source, count(*) FROM docs GROUP BY source").fetchall()
    last = _meta_get(c, "last_index")
    c.close()
    return {"indexed": True, "db": str(DB),
            "mb": round(DB.stat().st_size / 1e6, 1),
            "counts": {r[0]: r[1] for r in rows},
            "last_index": (time.strftime("%Y-%m-%d %H:%M",
                                         time.localtime(last)) if last else None)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("index"); pi.add_argument("--full", action="store_true")
    ps = sub.add_parser("search"); ps.add_argument("q")
    ps.add_argument("--source", choices=["jarvis", "memory", "vault", "file"])
    ps.add_argument("-k", type=int, default=6)
    sub.add_parser("status")
    a = p.parse_args()

    if a.cmd == "index":
        print(json.dumps(reindex(a.full, progress=True), indent=2))
    elif a.cmd == "search":
        print(json.dumps(search(a.q, a.source, a.k), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(status(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
