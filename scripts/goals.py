#!/usr/bin/env python3
"""Jarvis goal store — the top-3 King sets before bed, read back at wake.

  goals.py set --goal "..." --step "..." --step "..." [--step "..."] [--for tomorrow]
  goals.py show [--for today]
  goals.py list
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys

from common import GOALS_FILE, read_json, target_date, write_json


def _load() -> dict:
    data = read_json(GOALS_FILE, default={})
    return data if isinstance(data, dict) else {}


def cmd_set(args) -> int:
    if not args.step:
        print("error: at least one --step is required", file=sys.stderr)
        return 2
    if len(args.step) > 5:
        print("error: keep it to 5 steps or fewer — it's a top-3, not a backlog",
              file=sys.stderr)
        return 2

    day = target_date(args.for_)
    store = _load()
    store[day.isoformat()] = {
        "date": day.isoformat(),
        "main_goal": args.goal,
        "steps": args.step,
        "notes": args.notes or "",
        "set_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    write_json(GOALS_FILE, store)
    print(json.dumps(store[day.isoformat()], indent=2, ensure_ascii=False))
    return 0


def cmd_show(args) -> int:
    day = target_date(args.for_ or "today")
    store = _load()
    rec = store.get(day.isoformat())
    if rec is None:
        # Fall back to the most recent record so the caller can flag it stale
        # rather than brief with no spine at all.
        keys = sorted(k for k in store if k <= day.isoformat())
        if keys:
            rec = dict(store[keys[-1]])
            rec["stale"] = True
            rec["stale_by_days"] = (day - _dt.date.fromisoformat(keys[-1])).days
    print(json.dumps(rec, indent=2, ensure_ascii=False))
    return 0


def cmd_list(_args) -> int:
    print(json.dumps(_load(), indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("set")
    s.add_argument("--goal", required=True, help="the single main goal for the day")
    s.add_argument("--step", action="append", default=[],
                   help="a step toward the goal; repeat (2-3 is the sweet spot)")
    s.add_argument("--notes", default="")
    s.add_argument("--for", dest="for_", default=None,
                   help="today | tomorrow | YYYY-MM-DD (default: tomorrow after 6pm)")
    s.set_defaults(func=cmd_set)

    sh = sub.add_parser("show")
    sh.add_argument("--for", dest="for_", default=None)
    sh.set_defaults(func=cmd_show)

    sub.add_parser("list").set_defaults(func=cmd_list)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
