"""Shared helpers for the Jarvis skill: state dir, .env loading, date math."""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

# A console program (ffmpeg, powershell, cmd) launched from a parent that has no
# console of its own gets a BRAND NEW console window allocated by Windows. The ears
# run under pythonw.exe via the "Jarvis Server" scheduled task, and capture() shells
# out to ffmpeg every 5 seconds forever — so that is a black window flashing on the
# desktop every 5 seconds, permanently. CREATE_NO_WINDOW suppresses the allocation.
#
# EVERY subprocess call in this skill must pass **NO_WINDOW. There is no exception:
# ffmpeg, powershell and cmd all behave this way, and any new one will too.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

# Windows consoles default to cp1252, which cannot encode the arrows, emoji and
# em-dashes that live in hot.md and in a briefing. Without this every script dies
# on print() the moment real content shows up. Importing common.py fixes it once.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # not a reconfigurable TextIO
        pass

# Bump this on every code change that reaches the desktop. The HUD renders it
# under the JARVIS wordmark so King can glance at the running instance and know
# whether it is current — the "Jarvis Server" scheduled task only picks up new
# code on restart, and a stale-but-healthy process gives no other visible sign.
VERSION = "1.9.2"

STATE_DIR = Path.home() / ".jarvis"
GOALS_FILE = STATE_DIR / "goals.json"
BRIEF_FILE = STATE_DIR / "brief.json"
HUD_FILE = STATE_DIR / "hud.html"

def _env_candidates() -> list[Path]:
    """.env files searched in priority order; first hit for a key wins."""
    from config import cfg_paths
    return [STATE_DIR / ".env",
            Path.home() / ".claude" / "skills" / "jarvis" / ".env",
            *cfg_paths("extra_env_files")]


def hot_md_path() -> Path:
    """The owner's second-brain hot.md — per-machine, so a function rather
    than a constant read at import time (config.json may not exist yet)."""
    from config import cfg_path
    return cfg_path("hot_md")

# King's browser identity. He runs ~28 Chrome profiles; a launch without an
# explicit profile flag opens whichever was used LAST — roulette that dropped
# the HUD and music tabs into random business identities. "Default" is his
# main profile ("Person 1", signed in as cfmbusiness@gmail.com — his CFM
# business browser, King's instruction 2026-08-15). Every Chrome launch in
# this skill must carry it.
CHROME_PROFILE = "Default"

CHROME_PATHS = [
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
]


def open_in_chrome(url: str, app: bool = False,
                   window_size: str | None = None) -> bool:
    """Open a URL in King's CFM business Chrome profile.

    app=True gives the chromeless HUD window; False a normal tab. Returns
    False when chrome.exe is missing so the caller can fall back to
    webbrowser.open — which loses profile pinning but never loses the page.
    """
    for exe in CHROME_PATHS:
        if exe.exists():
            args = [str(exe), f"--profile-directory={CHROME_PROFILE}"]
            if app:
                args.append(f"--app={url}")
                if window_size:
                    args.append(f"--window-size={window_size}")
            else:
                args.append(url)
            subprocess.Popen(args, **NO_WINDOW)
            return True
    return False


def ensure_state_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


def load_env_key(name: str) -> str | None:
    """Return a key from the real environment, else the first .env file that has it.

    Values are never logged or printed by any script in this skill.
    """
    val = os.environ.get(name)
    if val:
        return val.strip().strip('"').strip("'") or None
    for path in _env_candidates():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            if key.strip() == name:
                got = raw.strip().strip('"').strip("'")
                if got:
                    return got
    return None


def today() -> _dt.date:
    return _dt.datetime.now().date()


def target_date(for_arg: str | None) -> _dt.date:
    """Resolve --for. Default flips to tomorrow after 18:00 local, matching the
    'tell it my top three before bed' habit the briefing is built around."""
    now = _dt.datetime.now()
    if for_arg in (None, "", "auto"):
        return now.date() + _dt.timedelta(days=1) if now.hour >= 18 else now.date()
    if for_arg == "today":
        return now.date()
    if for_arg == "tomorrow":
        return now.date() + _dt.timedelta(days=1)
    return _dt.date.fromisoformat(for_arg)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload) -> None:
    ensure_state_dir()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
