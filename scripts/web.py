#!/usr/bin/env python3
"""Pillar 7 — the internet. Search, read, answer with sources.

  web.py search "kokoro tts pricing"
  web.py answer "what's the latest on the NHTSA recall database API"

Search order: ddgs (free, no key) -> Tavily (key in ~/.scraper-stack.env).
Pages are fetched directly and stripped to prose; the answer model is grounded
on ONLY what was fetched, exactly like the local brain — a web answer with no
sources behind it is not an answer, it is the model free-associating.

This module NEVER acts on the web — it reads. Anything transactional (buying,
posting, logging in) stays with Claude Code and the browser where King can see
it happen.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

from common import load_env_key

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"   # Cloudflare-1010 lesson: never python-urllib's default
_TAG = re.compile(r"(?is)<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>")
_HTML = re.compile(r"(?s)<[^>]+>")

SCRAPER_ENV = Path.home() / ".scraper-stack.env"


def _scraper_key(name: str) -> str | None:
    val = load_env_key(name)
    if val:
        return val
    try:
        for line in SCRAPER_ENV.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def _ddg(query: str, k: int) -> list[dict]:
    from ddgs import DDGS
    out = []
    with DDGS() as d:
        for r in d.text(query, max_results=k):
            out.append({"title": r.get("title", ""), "url": r.get("href", ""),
                        "snippet": r.get("body", "")})
    return out


def _tavily(query: str, k: int) -> list[dict]:
    key = _scraper_key("TAVILY_API_KEY")
    if not key:
        return []
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps({"api_key": key, "query": query,
                         "max_results": k}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    return [{"title": x.get("title", ""), "url": x.get("url", ""),
             "snippet": x.get("content", "")} for x in data.get("results", [])]


def search(query: str, k: int = 5) -> list[dict]:
    """Web results, or [] — never an exception. Empty means 'say so', not guess."""
    for engine in (_ddg, _tavily):
        try:
            hits = engine(query, k)
            if hits:
                return hits
        except Exception as exc:
            print(f"[web] {engine.__name__} failed: {exc}", file=sys.stderr)
    return []


def fetch(url: str, chars: int = 4000) -> str:
    """One page as prose. Empty string on any failure — a page that cannot be
    read is simply not a source, the same rule the local brain applies."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(600_000).decode("utf-8", "ignore")
        text = _HTML.sub(" ", _TAG.sub(" ", raw))
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text[:chars]
    except Exception as exc:
        print(f"[web] fetch {url[:60]}: {exc}", file=sys.stderr)
        return ""


def gather(query: str, k: int = 4, chars: int = 3000) -> tuple[str, list[dict]]:
    """(sources_block, hits) — search, read the top pages, build the grounding.

    Snippets alone are headline-bait; the fetched page is what actually
    supports an answer. A page that will not fetch degrades to its snippet.
    """
    hits = search(query, k)
    blocks, used = [], []
    for h in hits:
        body = fetch(h["url"], chars) or h["snippet"]
        if len(body) < 80:
            continue
        blocks.append(f"[WEB: {h['title']} — {h['url']}]\n{body}")
        used.append(h)
        if len(blocks) >= k:
            break
    return "\n\n".join(blocks)[:12000], used


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("search"); ps.add_argument("q"); ps.add_argument("-k", type=int, default=5)
    pa = sub.add_parser("answer"); pa.add_argument("q")
    a = p.parse_args()

    if a.cmd == "search":
        print(json.dumps(search(a.q, a.k), indent=2, ensure_ascii=False))
    else:
        import listen
        print(listen.web_answer(a.q))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
