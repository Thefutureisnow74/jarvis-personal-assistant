#!/usr/bin/env python3
"""Prove — not assume — that Jarvis can hear and be heard.

Silence alone tells you nothing: a quiet room and a dead microphone both read
about -60 dB. So this plays a known tone through the default output device while
recording, which tests the whole physical chain at once (speaker -> room -> mic).

  miccheck.py            # tone test, then a live meter you speak into
  miccheck.py --tone     # tone test only
  miccheck.py --meter 15 # live meter only, 15 seconds
"""
from __future__ import annotations

import argparse
import math
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

from common import NO_WINDOW, ensure_state_dir
import listen

TONE = Path(tempfile.gettempdir()) / "jarvis_tone.wav"


def make_tone(seconds: float = 3.0, freq: int = 440, rate: int = 16000) -> Path:
    """Full-scale-ish sine. Loud on purpose: the point is to be unmissable."""
    frames = bytearray()
    for i in range(int(rate * seconds)):
        v = int(26000 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", v)
    with wave.open(str(TONE), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(bytes(frames))
    return TONE


def play_default(path: Path) -> None:
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "Add-Type -AssemblyName presentationCore; "
                    "$p = New-Object System.Windows.Media.MediaPlayer; "
                    f"$p.Open([uri]'{path}'); Start-Sleep -Milliseconds 400; "
                    "$p.Volume = 1; $p.Play(); Start-Sleep -Seconds 4; $p.Close()"],
                   capture_output=True, timeout=90, **NO_WINDOW)


def tone_test() -> int:
    devices = listen.list_devices()
    if not devices:
        print("No microphones found at all.")
        return 2
    make_tone()
    print("\n  Playing a 440 Hz tone through the DEFAULT output device.")
    print("  If you hear nothing, Windows is sending audio somewhere you are not")
    print("  listening — check the volume flyout for a connected headset.\n")

    results = []
    for d in devices:
        t = threading.Thread(target=play_default, args=(TONE,), daemon=True)
        t.start()
        time.sleep(0.8)
        tmp = Path(tempfile.gettempdir()) / "jarvis_check.wav"
        listen.capture(d, 3, tmp)
        rms = listen.loudness(tmp)
        lvl = listen.device_level(d, 0.4)
        results.append((rms, d))
        verdict = "HEARD THE TONE" if rms >= 200 else "heard nothing"
        print(f"  {rms:>6} peak RMS   {verdict:<15}  {d[:52]}")
        tmp.unlink(missing_ok=True)
        t.join(timeout=20)

    best = max(results)[0]
    print()
    if best >= 200:
        print("  RESULT: the speaker -> room -> microphone chain WORKS.")
        print("          Jarvis can hear, and audio is coming out of the laptop.")
    else:
        print("  RESULT: no microphone heard the tone. Either the sound went to a")
        print("          headset/Bluetooth device instead of the speakers, or every")
        print("          microphone is muted. Check the Windows volume flyout first —")
        print("          a connected headset is the usual cause and explains BOTH")
        print("          'it will not talk' and 'it cannot hear me'.")
    return 0


def meter(seconds: int, device: str | None) -> int:
    d = device or listen.resolve_device(None)
    if not d:
        print("No microphone found.")
        return 2
    print(f"\n  Live meter on: {d}")
    print(f"  SPEAK NOW — say \"Jarvis, brief me\" a few times. {seconds}s.\n")
    tmp = Path(tempfile.gettempdir()) / "jarvis_meter.wav"
    peak = 0
    end = time.time() + seconds
    while time.time() < end:
        if not listen.capture(d, 2, tmp):
            print("  capture failed"); break
        rms = listen.loudness(tmp)
        peak = max(peak, rms)
        bar = "#" * min(int(rms / 60), 52)
        flag = "  <-- clears the gate" if rms >= listen.RMS_GATE else ""
        print(f"  {rms:>6} |{bar:<52}|{flag}")
    tmp.unlink(missing_ok=True)
    print(f"\n  peak: {peak}   gate: {listen.RMS_GATE}")
    if peak >= listen.RMS_GATE:
        print("  RESULT: your voice clears the gate. Jarvis can hear you.")
    elif peak > 40:
        print(f"  RESULT: the mic hears you but too quietly. Run the server with")
        print(f"          --gate {max(int(peak * 0.6), 30)} , or raise the microphone")
        print(f"          level in Windows sound settings.")
    else:
        print("  RESULT: essentially nothing reached the microphone.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tone", action="store_true")
    p.add_argument("--meter", type=int, nargs="?", const=12, default=None)
    p.add_argument("--device", default=None)
    a = p.parse_args()
    ensure_state_dir()
    if a.meter is not None and not a.tone:
        return meter(a.meter, a.device)
    rc = tone_test()
    if not a.tone:
        rc = meter(12, a.device) or rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
