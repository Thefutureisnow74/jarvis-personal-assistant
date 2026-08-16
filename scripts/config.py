"""Per-machine configuration — the one file that makes Jarvis portable.

Everything owner-specific lives here and NOWHERE else: the vault location, the
owner's name, the Chrome profile, the microphone. Read from
``~/.jarvis/config.json`` (written by ``install_jarvis.py``), with every key
falling back to the value the original machine used.

That fallback is deliberate. The first Jarvis ran with these paths baked into
five modules; making them configurable had to be a change no existing install
could notice. No config file at all == the original behaviour, exactly.

Add a setting here, read it via ``cfg()``, and never hardcode a personal path
in a module again — the next install is somebody else's computer.
"""
from __future__ import annotations

import json
from pathlib import Path

CONFIG_FILE = Path.home() / ".jarvis" / "config.json"

# The original machine's values. A key missing from config.json resolves here,
# so the very first install keeps working with no config file present.
DEFAULTS = {
    "owner_name": "King",
    "owner_address": "sir",
    "vault_dir": "C:/Users/12149/Documents/Hermes Agents/ObsiVault",
    "hot_md": "C:/Users/12149/Documents/Hermes Agents/ObsiVault/hot.md",
    # Extra .env files to search after ~/.jarvis/.env (first hit wins).
    "extra_env_files": ["C:/Users/12149/Documents/Hermes Agents/.env"],
    # Claude Code auto-memory folders to index as the `memory` corpus.
    "memory_dirs": [str(Path.home() / ".claude" / "projects"
                        / "c--Users-12149-Documents-Hermes-Agents" / "memory")],
    # Extra Google OAuth token files to try beyond Jarvis's own.
    "extra_google_tokens": [
        "C:/Users/12149/Documents/Hermes Agents/Lisa_Hermes/cfmbusiness_google_token.json",
        str(Path.home() / ".config" / "google-workspace-mcp" / "tokens.json"),
        "C:/Users/12149/.mer/cfmbusiness_google_token.json",
    ],
    "chrome_profile": "Default",
    "mic_device": "EarPods",     # substring; "" = auto-pick the loudest
    "port": 8787,
}

_cache: dict | None = None


def all_settings() -> dict:
    global _cache
    if _cache is None:
        merged = dict(DEFAULTS)
        try:
            merged.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass                      # no config == original machine's defaults
        _cache = merged
    return _cache


def cfg(key: str, default=None):
    return all_settings().get(key, DEFAULTS.get(key, default))


def cfg_path(key: str) -> Path:
    return Path(str(cfg(key))).expanduser()


def cfg_paths(key: str) -> list[Path]:
    return [Path(str(p)).expanduser() for p in (cfg(key) or [])]


def write(settings: dict) -> Path:
    """Persist config and drop the cache so a running process sees it."""
    global _cache
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    _cache = None
    return CONFIG_FILE
