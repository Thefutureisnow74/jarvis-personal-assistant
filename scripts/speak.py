#!/usr/bin/env python3
"""Pillar 4 — the voice.

Providers, best first:
  elevenlabs  used when ELEVENLABS_API_KEY resolves. Set ELEVENLABS_VOICE_ID to a
              cloned voice for the Paul-Bettany-style Jarvis.
  sapi        Windows System.Speech. Always present, zero setup, robotic.

The provider that actually spoke is always reported on stdout as JSON, so the caller
can never claim a cloned voice it did not use.

  speak.py "Good morning, sir."
  speak.py --from-brief ~/.jarvis/brief.json
  speak.py --status
  speak.py --provider sapi --voice "Microsoft David Desktop" "text"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from common import NO_WINDOW, STATE_DIR, ensure_state_dir, load_env_key, read_json

DEFAULT_SAPI_VOICE = "Microsoft David Desktop"
ELEVEN_DEFAULT_VOICE = "onwK4e9ZLuTAKqWW03F9"  # Daniel — British, closest stock match
ELEVEN_MODEL = "eleven_turbo_v2_5"

# The Jarvis voice. Kokoro-82M on DeepInfra: British male, measured, ~$0.62 per
# million characters — about two cents a month at one briefing a day. Audition the
# alternatives with --preset: bm_george (default), bm_lewis, bm_daniel, bm_fable.
DEEPINFRA_MODEL = "hexgrad/Kokoro-82M"
DEEPINFRA_VOICE = "bm_george"


# ----------------------------------------------------------------- providers

def _resolve_provider(forced: str | None) -> tuple[str, str | None]:
    """Pick the best available voice. Order: elevenlabs > deepinfra > sapi."""
    eleven = load_env_key("ELEVENLABS_API_KEY")
    deep = load_env_key("DEEPINFRA_API_KEY")

    if forced == "sapi":
        return "sapi", "forced by --provider sapi"
    if forced == "elevenlabs":
        return ("elevenlabs", None) if eleven else \
            ("sapi", "forced elevenlabs but ELEVENLABS_API_KEY is not set")
    if forced == "deepinfra":
        return ("deepinfra", None) if deep else \
            ("sapi", "forced deepinfra but DEEPINFRA_API_KEY is not set")

    if eleven:
        return "elevenlabs", None
    if deep:
        return "deepinfra", None
    return "sapi", "no ELEVENLABS_API_KEY or DEEPINFRA_API_KEY — falling back to Windows SAPI"


def _speak_deepinfra(text: str, out_wav: Path, preset: str) -> dict:
    """Kokoro-82M via DeepInfra. Returns wav bytes base64 in the JSON body."""
    import base64
    key = load_env_key("DEEPINFRA_API_KEY")
    req = urllib.request.Request(
        f"https://api.deepinfra.com/v1/inference/{DEEPINFRA_MODEL}",
        data=json.dumps({"text": text, "preset_voice": [preset]}).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "provider": "deepinfra",
                "error": f"HTTP {exc.code}: {exc.read().decode('utf-8','ignore')[:220]}"}
    except Exception as exc:
        return {"ok": False, "provider": "deepinfra", "error": str(exc)}

    audio = data.get("audio") or ""
    if not audio:
        return {"ok": False, "provider": "deepinfra",
                "error": f"no audio in response; keys={list(data)[:6]}"}
    raw = audio.split(",", 1)[1] if audio.startswith("data:") else audio
    out_wav.write_bytes(base64.b64decode(raw))

    play = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Add-Type -AssemblyName presentationCore; "
         "$p = New-Object System.Windows.Media.MediaPlayer; "
         f"$p.Open([uri]{json.dumps(str(out_wav))}); Start-Sleep -Milliseconds 500; "
         "$p.Play(); "
         "while ($p.NaturalDuration.HasTimeSpan -eq $false) { Start-Sleep -Milliseconds 100 }; "
         "while ($p.Position -lt $p.NaturalDuration.TimeSpan) { Start-Sleep -Milliseconds 150 }; "
         "$p.Close()"],
        capture_output=True, text=True, timeout=900, **NO_WINDOW)
    return {"ok": True, "provider": "deepinfra", "voice": preset,
            "model": DEEPINFRA_MODEL, "audio": str(out_wav),
            "playback": "ok" if play.returncode == 0
                        else (play.stderr or "").strip()[:250]}


def _speak_sapi(text: str, voice: str, rate: int, out_wav: Path | None) -> dict:
    """Drive System.Speech via PowerShell. Text goes over stdin, never the command
    line — a briefing contains quotes, newlines and $ signs that would maul an
    interpolated argument.

    SetOutputToWaveFile *redirects* the synthesiser, so a saved run is a silent run.
    Speak aloud first, then synthesise a second pass to disk only if a copy was asked
    for.
    """
    save = (f"$s.SetOutputToWaveFile({json.dumps(str(out_wav))}); $s.Speak($t); "
            "$s.SetOutputToNull()") if out_wav else ""
    ps = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$t = [Console]::In.ReadToEnd()
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{ $s.SelectVoice({json.dumps(voice)}) }} catch {{ }}
$s.Rate = {int(rate)}
$s.SetOutputToDefaultAudioDevice()
$s.Speak($t)
{save}
$s.Dispose()
"""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        input=text, capture_output=True, text=True, timeout=300, **NO_WINDOW,
    )
    if proc.returncode != 0:
        return {"ok": False, "provider": "sapi",
                "error": (proc.stderr or "").strip()[:400]}
    return {"ok": True, "provider": "sapi", "voice": voice,
            "wav": str(out_wav) if out_wav else None}


def _speak_elevenlabs(text: str, out_wav: Path) -> dict:
    key = load_env_key("ELEVENLABS_API_KEY")
    voice_id = load_env_key("ELEVENLABS_VOICE_ID") or ELEVEN_DEFAULT_VOICE
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=json.dumps({
            "text": text,
            "model_id": load_env_key("ELEVENLABS_MODEL_ID") or ELEVEN_MODEL,
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.8,
                               "style": 0.35, "use_speaker_boost": True},
        }).encode("utf-8"),
        headers={"xi-api-key": key, "Content-Type": "application/json",
                 "Accept": "audio/mpeg"},
        method="POST",
    )
    mp3 = out_wav.with_suffix(".mp3")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            mp3.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        # Body may name the real cause (quota, bad voice id) — surface it, minus the key.
        detail = exc.read().decode("utf-8", "ignore")[:300]
        return {"ok": False, "provider": "elevenlabs",
                "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "provider": "elevenlabs", "error": str(exc)}

    play = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Add-Type -AssemblyName presentationCore; "
         "$p = New-Object System.Windows.Media.MediaPlayer; "
         f"$p.Open([uri]{json.dumps(str(mp3))}); Start-Sleep -Milliseconds 700; "
         "$p.Play(); "
         "while ($p.Position -lt $p.NaturalDuration.TimeSpan) { Start-Sleep -Milliseconds 200 }; "
         "$p.Close()"],
        capture_output=True, text=True, timeout=600, **NO_WINDOW,
    )
    return {"ok": True, "provider": "elevenlabs", "voice_id": voice_id,
            "audio": str(mp3),
            "playback": "ok" if play.returncode == 0
                        else (play.stderr or "").strip()[:300]}


# ---------------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("text", nargs="?", help="text to speak (or use --from-brief/stdin)")
    p.add_argument("--from-brief", dest="brief",
                   help="read the 'spoken' field out of a brief.json")
    p.add_argument("--provider", choices=["elevenlabs", "deepinfra", "sapi"], default=None)
    p.add_argument("--voice", default=DEFAULT_SAPI_VOICE, help="SAPI voice name")
    p.add_argument("--preset", default=DEEPINFRA_VOICE,
                   help="Kokoro voice: bm_george | bm_lewis | bm_daniel | bm_fable")
    p.add_argument("--rate", type=int, default=0, help="SAPI rate, -10..10")
    p.add_argument("--save", help="also keep the audio at this path")
    p.add_argument("--mute", action="store_true",
                   help="resolve the provider and echo the text, but stay silent")
    p.add_argument("--status", action="store_true",
                   help="print which provider would be used, and exit")
    args = p.parse_args()

    provider, reason = _resolve_provider(args.provider)

    if args.status:
        print(json.dumps({
            "provider": provider,
            "reason": reason,
            "elevenlabs_key_present": bool(load_env_key("ELEVENLABS_API_KEY")),
            "elevenlabs_voice_id": load_env_key("ELEVENLABS_VOICE_ID"),
            "sapi_default_voice": DEFAULT_SAPI_VOICE,
            "cloned_voice": provider == "elevenlabs",
        }, indent=2))
        return 0

    if args.brief:
        brief = read_json(Path(args.brief), default=None)
        if not brief or not brief.get("spoken"):
            print(json.dumps({"ok": False,
                              "error": f"no 'spoken' field in {args.brief}"}),
                  file=sys.stderr)
            return 2
        text = brief["spoken"]
    elif args.text:
        text = args.text
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        p.error("give text, --from-brief, or pipe it in")

    text = text.strip()
    if not text:
        print(json.dumps({"ok": False, "error": "empty text"}), file=sys.stderr)
        return 2

    if args.mute:
        print(json.dumps({"ok": True, "provider": provider, "muted": True,
                          "chars": len(text)}, indent=2))
        return 0

    ensure_state_dir()
    save_to = Path(args.save) if args.save else None

    default_wav = save_to or STATE_DIR / "last_brief_audio.wav"

    if provider == "elevenlabs":
        # ElevenLabs always lands a file first — the file *is* the playback source.
        result = _speak_elevenlabs(text, default_wav)
    elif provider == "deepinfra":
        result = _speak_deepinfra(text, default_wav, args.preset)
    else:
        result = _speak_sapi(text, args.voice, args.rate, save_to)

    if not result.get("ok") and provider != "sapi":
        # A dead key, a blown quota or a network blip must never leave Jarvis mute.
        fallback = _speak_sapi(text, args.voice, args.rate, save_to)
        fallback["fell_back_from"] = result
        result = fallback

    result["reason"] = reason
    result["cloned_voice"] = result.get("provider") == "elevenlabs"
    result["chars"] = len(text)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
