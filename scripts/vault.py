#!/usr/bin/env python3
"""Reads King's ObsiVault wiki into a graph the HUD can render.

Every page is a node, every [[wikilink]] an edge. This is what replaces the
abstract particle sphere at the centre of the display — the orb IS the second
brain, so it grows as the vault does.

  vault.py                 # write ~/.jarvis/vault_graph.json, print a summary
  vault.py --stdout        # print the graph JSON instead
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from common import STATE_DIR, ensure_state_dir
from config import cfg_path

VAULT = cfg_path("vault_dir")
GRAPH = STATE_DIR / "vault_graph.json"
PAGES = STATE_DIR / "vault_pages.json"   # id -> absolute path, for query/read

LINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
# raw/ holds immutable source dumps, not wiki pages — including them would drown
# the graph in material that has no outgoing links.
SKIP = {"raw", ".git", ".obsidian", "templates"}

GROUPS = {"entities": "entity", "concepts": "concept", "sources": "source",
          "syntheses": "synthesis", "topics": "topic", "queries": "query"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def build() -> dict:
    if not VAULT.exists():
        return {"nodes": [], "links": [], "error": f"vault not found: {VAULT}"}

    pages: dict[str, dict] = {}
    paths: dict[str, str] = {}
    raw_links: list[tuple[str, str]] = []

    for f in VAULT.rglob("*.md"):
        if any(part in SKIP for part in f.relative_to(VAULT).parts):
            continue
        rel = f.relative_to(VAULT)
        group = next((g for d, g in GROUPS.items() if d in rel.parts), "core")
        key = slug(f.stem)
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        pages[key] = {"id": key, "title": f.stem.replace("-", " "), "group": group}
        paths[key] = str(f)
        for m in LINK.finditer(text):
            raw_links.append((key, slug(m.group(1))))

    # A [[link]] to a page that does not exist yet is deliberate in this vault —
    # it marks something worth writing. Keep it as a ghost node so the gaps show.
    for _, dst in raw_links:
        if dst and dst not in pages:
            pages[dst] = {"id": dst, "title": dst.replace("-", " "), "group": "ghost"}

    seen, links = set(), []
    for src, dst in raw_links:
        if not dst or src == dst:
            continue
        pair = tuple(sorted((src, dst)))
        if pair in seen:
            continue
        seen.add(pair)
        links.append({"s": src, "t": dst})

    deg: dict[str, int] = {}
    for l in links:
        deg[l["s"]] = deg.get(l["s"], 0) + 1
        deg[l["t"]] = deg.get(l["t"], 0) + 1
    for k, p in pages.items():
        p["deg"] = deg.get(k, 0)

    nodes = sorted(pages.values(), key=lambda p: -p["deg"])
    return {"nodes": nodes, "links": links, "paths": paths,
            "counts": {"pages": sum(1 for n in nodes if n["group"] != "ghost"),
                       "ghosts": sum(1 for n in nodes if n["group"] == "ghost"),
                       "links": len(links)}}


# ------------------------------------------------------------------ query API

# find_page() lived here: a filename-only fuzzy match that answered a spoken topic
# with one best-guess page. It was REMOVED 2026-08-15 and deliberately not replaced
# in kind — matching titles meant any answer living in the BODY of a differently
# named page came back as "I have nothing on that in the vault, sir", which is the
# most misleading thing this system can say. Content search now lives in brain.py
# (SQLite FTS5, bm25); use brain.search(topic, source="vault").


def page_text(pid: str) -> str:
    pages = json.loads(PAGES.read_text(encoding="utf-8")) if PAGES.exists() else {}
    path = pages.get(pid)
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stdout", action="store_true")
    args = p.parse_args()

    g = build()
    if args.stdout:
        print(json.dumps(g, indent=2, ensure_ascii=False))
        return 0
    ensure_state_dir()
    paths = g.pop("paths", {})
    GRAPH.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    PAGES.write_text(json.dumps(paths, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": "error" not in g, "graph": str(GRAPH),
                      **g.get("counts", {}), "error": g.get("error")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
