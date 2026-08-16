#!/usr/bin/env python3
"""Music — Jarvis puts something on.

  music.py play "miles davis kind of blue"    # queue ~12 tracks, autoplay
  music.py play "so what" --one               # just that one
  music.py pause                              # toggle play/pause
  music.py next
  music.py stop

HOW IT PLAYS: yt-dlp resolves the search to video IDs without downloading
anything (--flat-playlist is a metadata call), then the default browser opens
    youtube.com/watch_videos?video_ids=a,b,c
which YouTube turns into a temporary autoplaying playlist. No API key, no OAuth,
no quota — the YouTube Data API would need a key and a 10,000-unit daily budget
to do strictly less.

HOW IT STOPS: Windows media keys, sent with keybd_event. This is the honest
mechanism — Jarvis has no handle on the browser tab, but every browser honours
the OS play/pause key. Killing the browser process instead would take King's
other tabs with it.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from common import NO_WINDOW

# WinGet puts yt-dlp on PATH for an interactive shell, but the "Jarvis Server"
# scheduled task starts with a thinner environment — resolve the real path too.
# Globbed under the CURRENT user's WinGet packages, not a fixed username, so
# this keeps working on whichever machine Jarvis is installed on.
YTDLP_FALLBACK = next(
    (Path.home() / "AppData/Local/Microsoft/WinGet/Packages")
    .glob("yt-dlp.yt-dlp_Microsoft.Winget.Source_*/yt-dlp.exe"), None)

VK = {"pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2}


def ytdlp() -> str | None:
    return shutil.which("yt-dlp") or (str(YTDLP_FALLBACK)
                                      if YTDLP_FALLBACK and YTDLP_FALLBACK.exists()
                                      else None)


def resolve(query: str, n: int = 12) -> list[dict]:
    """Search YouTube and return [{id, title}]. Never downloads media."""
    exe = ytdlp()
    if not exe:
        return []
    try:
        r = subprocess.run(
            [exe, f"ytsearch{n}:{query}", "--flat-playlist", "--no-warnings",
             "--print", "%(id)s\t%(title)s"],
            capture_output=True, text=True, timeout=90, encoding="utf-8",
            errors="replace", **NO_WINDOW)
    except Exception as exc:
        print(f"[music] yt-dlp failed: {exc}", file=sys.stderr)
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        vid, _, title = line.partition("\t")
        vid = vid.strip()
        if len(vid) == 11:                      # a YouTube id is always 11 chars
            out.append({"id": vid, "title": title.strip()})
    if not out and r.stderr:
        print(f"[music] {r.stderr.strip()[:200]}", file=sys.stderr)
    return out


def play(query: str, one: bool = False, n: int = 12) -> dict:
    tracks = resolve(query, 1 if one else n)
    if not tracks:
        return {"ok": False, "error": f"nothing found on YouTube for {query!r}"}
    if one or len(tracks) == 1:
        url = f"https://www.youtube.com/watch?v={tracks[0]['id']}"
    else:
        ids = ",".join(t["id"] for t in tracks)
        url = f"https://www.youtube.com/watch_videos?video_ids={ids}"
    # King's CFM business Chrome profile, not last-used-profile roulette.
    from common import open_in_chrome
    if not open_in_chrome(url):
        webbrowser.open(url)
    return {"ok": True, "playing": tracks[0]["title"], "queued": len(tracks),
            "url": url, "tracks": [t["title"] for t in tracks]}


def media_key(which: str) -> bool:
    """Send a virtual media key. Works on whatever is playing, browser included."""
    code = VK.get(which)
    if code is None or os.name != "nt":
        return False
    ps = ("Add-Type -Name JK -Namespace Jarvis -MemberDefinition "
          "'[DllImport(\"user32.dll\")] public static extern void "
          "keybd_event(byte bVk, byte bScan, int dwFlags, int dwExtraInfo);'; "
          f"[Jarvis.JK]::keybd_event({code},0,0,0); "
          f"[Jarvis.JK]::keybd_event({code},0,2,0)")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=30, **NO_WINDOW)
    return r.returncode == 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("play"); pp.add_argument("query")
    pp.add_argument("--one", action="store_true")
    pp.add_argument("-n", type=int, default=12)
    for c in ("pause", "next", "prev", "stop"):
        sub.add_parser(c)
    a = p.parse_args()

    if a.cmd == "play":
        print(json.dumps(play(a.query, a.one, a.n), indent=2, ensure_ascii=False))
    else:
        print(json.dumps({"ok": media_key(a.cmd), "key": a.cmd}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
