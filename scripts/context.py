#!/usr/bin/env python3
"""Pillar 1 — life context. Prints the JSON block Jarvis briefs from.

Emits: clock, the goals record for the target day (flagged when stale), the head of
ObsiVault/hot.md, and a voice-provider readout so the wake never claims a cloned voice
it does not have.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

from common import GOALS_FILE, NO_WINDOW, hot_md_path, read_json, target_date

SCRIPTS = Path(__file__).resolve().parent


def _goals(day) -> dict | None:
    store = read_json(GOALS_FILE, default={}) or {}
    rec = store.get(day.isoformat())
    if rec is not None:
        return rec
    keys = sorted(k for k in store if k <= day.isoformat())
    if not keys:
        return None
    rec = dict(store[keys[-1]])
    rec["stale"] = True
    rec["stale_by_days"] = (day - _dt.date.fromisoformat(keys[-1])).days
    return rec


def _hot(limit: int) -> dict:
    hot_md = hot_md_path()
    try:
        text = hot_md.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {"available": False, "reason": str(exc), "path": str(hot_md)}
    lines = text.splitlines()
    return {
        "available": True,
        "path": str(hot_md),
        "mtime": _dt.datetime.fromtimestamp(
            hot_md.stat().st_mtime).isoformat(timespec="seconds"),
        "total_lines": len(lines),
        "head": "\n".join(lines[:limit]),
        "truncated": len(lines) > limit,
    }


def _voice() -> dict:
    try:
        out = subprocess.run(
            [sys.executable, str(SCRIPTS / "speak.py"), "--status"],
            capture_output=True, text=True, timeout=30, **NO_WINDOW,
        )
        return json.loads(out.stdout)
    except Exception as exc:  # provider probe must never break the wake
        return {"provider": "unknown", "error": str(exc)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--for", dest="for_", default="today")
    p.add_argument("--hot-lines", type=int, default=60)
    args = p.parse_args()

    now = _dt.datetime.now()
    day = target_date(args.for_)

    print(json.dumps({
        "clock": {
            "now": now.isoformat(timespec="seconds"),
            "weekday": now.strftime("%A"),
            "date": day.isoformat(),
            "part_of_day": ("morning" if now.hour < 12
                            else "afternoon" if now.hour < 17
                            else "evening"),
        },
        "goals": _goals(day),
        "hot_md": _hot(args.hot_lines),
        "voice": _voice(),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
