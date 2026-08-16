#!/usr/bin/env python3
"""JARVIS as one running application.

Runs the ears (microphone -> Whisper -> intents) and an HTTP server in a single
process, so the HUD stops being a static file and becomes the live front end:
the SEND box works, the panel shows what was just heard, and the display
re-renders whenever brief.json changes.

  serve.py                       # http://127.0.0.1:8787
  serve.py --port 9000
  serve.py --no-ears             # display only, microphone off
  serve.py --host 0.0.0.0        # reachable from your phone on the LAN

Routes
  GET  /               the HUD, rendered live from ~/.jarvis/brief.json
  GET  /api/status     {state, device, heard, reply, voice, ears}
  POST /api/say        {"text": "..."} -> runs the same intents as the voice path
  GET  /api/health     liveness

Binds to 127.0.0.1 by default: the page shows a full day, including anything
sensitive in the brief. --host 0.0.0.0 exposes it to every device on the network.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import brain
import hud
import listen
import vault
from common import (BRIEF_FILE, NO_WINDOW, STATE_DIR, ensure_state_dir,
                    load_env_key, read_json)

STATE = {"ears": False, "device": None}
GRAPH_FILE = STATE_DIR / "vault_graph.json"


class _TimestampedLog:
    """Prefixes every non-blank print() line with a clock time, so a log full
    of [jarvis] lines can actually be correlated to when King experienced
    something, not just what order things happened in."""
    def __init__(self, f):
        self._f = f

    def write(self, s: str) -> None:
        if s.strip():
            self._f.write(f"[{_dt.datetime.now():%H:%M:%S}] {s}")
        else:
            self._f.write(s)

    def flush(self) -> None:
        self._f.flush()


def vault_rescan_loop(interval: int = 180) -> None:
    """Keep the orb current: rebuild the graph whenever the ObsiVault changes.
    Cheap (a full parse is ~1s), so mtime-diffing the whole tree isn't worth the
    misses — just rebuild and only rewrite when the result differs.

    Also re-indexes the wiki AND the Claude Code auto-memory directory into the
    searchable brain on the same tick — both are small (113 + 185 files), so they
    ride the 3-minute tick rather than waiting on the 30-minute file sweep. A page
    or a memory King wrote five minutes ago is already answerable.
    """
    import time
    while True:
        time.sleep(interval)
        try:
            g = vault.build()
            paths = g.pop("paths", {})
            new = json.dumps(g, ensure_ascii=False)
            old = GRAPH_FILE.read_text(encoding="utf-8") if GRAPH_FILE.exists() else ""
            if new != old:
                GRAPH_FILE.write_text(new, encoding="utf-8")
                vault.PAGES.write_text(json.dumps(paths, ensure_ascii=False),
                                       encoding="utf-8")
                print(f"[jarvis] vault changed -> orb updated "
                      f"({g['counts']['pages']} pages, {g['counts']['links']} links)",
                      flush=True)
            c = brain.connect()
            r = brain.index_vault(c)
            rm = brain.index_memory(c)
            brain.index_jmem(c)      # hand-edited memory files; writes index themselves
            c.commit()
            c.close()
            if r.get("indexed"):
                print(f"[jarvis] {r['indexed']} vault page(s) re-indexed", flush=True)
            if rm.get("indexed"):
                print(f"[jarvis] {rm['indexed']} memory file(s) re-indexed", flush=True)
        except Exception as exc:
            print(f"[jarvis] vault rescan failed: {exc}", flush=True)


def file_reindex_loop(interval: int = 1800) -> None:
    """Keep the file index current. Incremental — an unchanged file costs one stat
    call — but 23,000 stats is still ~40 seconds, so this runs on its own slow
    timer rather than riding the vault tick every three minutes."""
    import time
    while True:
        time.sleep(interval)
        try:
            c = brain.connect()
            r = brain.index_files(c)
            c.commit()
            c.close()
            if r.get("indexed"):
                print(f"[jarvis] {r['indexed']} file(s) re-indexed", flush=True)
        except Exception as exc:
            print(f"[jarvis] file reindex failed: {exc}", flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "Jarvis/1.0"

    def log_message(self, *_a):            # keep the console for Jarvis, not for GETs
        pass

    # ------------------------------------------------------------------ helpers
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self) -> None:
        """Let the published here.now copy drive this local server.

        Browsers treat http://127.0.0.1 as a trustworthy origin, so an HTTPS page
        may call it — but only with CORS allowed, and Chrome sends a Private
        Network Access preflight first. Both are answered here. The server is
        bound to loopback, so 'any origin' still means 'a browser on this machine'.
        """
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    # ------------------------------------------------------------------ routes
    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html", "/phone"):
            brief = read_json(BRIEF_FILE, default=None)
            if brief is None:
                return self._send(200, b"<pre>No brief yet. Ask Claude Code to wake "
                                       b"Jarvis, then reload.</pre>", "text/html")
            # /phone: sphere + name + version only (King, 2026-08-16). The
            # laptop keeps the full display at / — same render, one flag.
            return self._send(200,
                              hud.render(brief, phone=(path == "/phone")).encode("utf-8"),
                              "text/html; charset=utf-8")

        if path == "/api/health":
            return self._json(200, {"ok": True})

        # PWA plumbing: lets "Add to Home Screen" on King's phone install the
        # HUD as a real full-screen app with an icon, not a browser shortcut.
        if path == "/manifest.json":
            return self._send(200, json.dumps({
                "name": "JARVIS", "short_name": "JARVIS",
                "start_url": "/phone", "display": "standalone",
                "background_color": "#05080d", "theme_color": "#05080d",
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                ]}).encode("utf-8"), "application/manifest+json")
        if path in ("/icon-192.png", "/icon-512.png"):
            f = STATE_DIR / path.lstrip("/")
            if f.exists():
                return self._send(200, f.read_bytes(), "image/png")
            return self._json(404, {"error": "icon not generated"})

        if path == "/api/status":
            st = read_json(listen.STATUS, default={}) or {}
            st["ears"] = STATE["ears"]
            st["device"] = STATE["device"]
            st["voice"] = ("elevenlabs" if load_env_key("ELEVENLABS_API_KEY")
                           else "deepinfra" if load_env_key("DEEPINFRA_API_KEY")
                           else "sapi")
            try:
                st["brief_mtime"] = BRIEF_FILE.stat().st_mtime
            except OSError:
                st["brief_mtime"] = 0
            try:
                st["graph_mtime"] = GRAPH_FILE.stat().st_mtime
            except OSError:
                st["graph_mtime"] = 0
            return self._json(200, st)

        if path.startswith("/vault/"):
            pid = path[len("/vault/"):].strip("/")
            text = vault.page_text(pid)
            if not text:
                return self._json(404, {"error": f"no vault page: {pid}"})
            body = ("<!doctype html><meta charset=utf-8><title>" + pid + "</title>"
                    "<body style='background:#05080d;color:#cfe8e4;font:13px/1.6 "
                    "Consolas,monospace;max-width:820px;margin:40px auto;padding:0 16px'>"
                    "<pre style='white-space:pre-wrap'>"
                    + text.replace("&", "&amp;").replace("<", "&lt;")
                    + "</pre></body>")
            return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/listen":
            # Push-to-talk: the browser's mic clip arrives as the raw body. ffmpeg
            # converts whatever the browser produced (webm/ogg/mp4) to the 16k mono
            # wav Whisper wants — never assume the container.
            import subprocess, tempfile, uuid as _uuid
            try:
                n = int(self.headers.get("Content-Length") or 0)
                blob = self.rfile.read(n)
            except Exception as exc:
                return self._json(400, {"error": f"bad audio: {exc}"})
            if len(blob) < 2000:
                return self._json(400, {"error": "clip too short"})

            tmpd = Path(tempfile.gettempdir())
            src = tmpd / f"jarvis_ptt_{_uuid.uuid4().hex[:8]}"
            wav = src.with_suffix(".wav")
            src.write_bytes(blob)
            try:
                r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                                    "-i", str(src), "-ac", "1", "-ar", "16000",
                                    "-y", str(wav)], capture_output=True, timeout=120,
                                   **NO_WINDOW)
                if r.returncode != 0 or not wav.exists():
                    return self._json(500, {"error": "could not decode the clip"})
                # Push-to-talk had NO loudness gate — only a byte-size check, which
                # a five-second recording of silence passes easily. Whisper then
                # invents filler for that silence and Jarvis answers it as a real
                # question. The ambient ears have gated on RMS since day one; this
                # path must too, or a muted/misrouted mic looks like hallucination
                # instead of like a dead microphone.
                rms = listen.loudness(wav)
                if rms < listen.RMS_GATE:
                    print(f"[jarvis] push-to-talk: silence (rms {rms} < "
                          f"{listen.RMS_GATE}) — mic not picking up", flush=True)
                    return self._json(200, {"ok": True, "heard": "", "rms": rms,
                                            "reply": "I am not hearing your "
                                                     "microphone, sir — nothing "
                                                     "came through."})
                text = listen.transcribe(wav)
            finally:
                src.unlink(missing_ok=True)
                wav.unlink(missing_ok=True)

            if not text or listen.is_noise_transcript(text):
                print(f"[jarvis] push-to-talk: filler transcript {text!r} ignored",
                      flush=True)
                return self._json(200, {"ok": True, "heard": "",
                                        "reply": "I did not catch that, sir."})
            m = listen.WAKE.match(text)
            command = text[m.end():].strip() if m else text.strip()
            if listen.is_noise_transcript(command):
                return self._json(200, {"ok": True, "heard": text,
                                        "reply": "I caught my name but not the "
                                                 "question, sir. Try me again."})
            print(f"[jarvis] > (push-to-talk) {command}", flush=True)
            try:
                reply = listen.handle(command)
            except Exception as exc:
                return self._json(500, {"error": str(exc)[:300]})
            print(f"[jarvis] < {reply}", flush=True)
            return self._json(200, {"ok": True, "heard": command, "reply": reply})

        if path != "/api/say":
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            text = (json.loads(self.rfile.read(n) or b"{}").get("text") or "").strip()
        except Exception as exc:
            return self._json(400, {"error": f"bad request: {exc}"})
        if not text:
            return self._json(400, {"error": "empty text"})

        # Typing into the HUD is the same as speaking: strip an optional wake word
        # so "Jarvis, brief me" and "brief me" both work.
        m = listen.WAKE.match(text)
        command = text[m.end():].strip() if m else text
        print(f"[jarvis] > (typed) {command}", flush=True)
        try:
            reply = listen.handle(command)
        except Exception as exc:
            return self._json(500, {"error": str(exc)[:300]})
        print(f"[jarvis] < {reply}", flush=True)
        return self._json(200, {"ok": True, "heard": command, "reply": reply})


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--device", default=None, help="microphone (substring is enough)")
    p.add_argument("--no-ears", action="store_true", help="display only, no microphone")
    p.add_argument("--gate", type=int, default=listen.RMS_GATE)
    p.add_argument("--window", type=int, default=listen.WINDOW)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    ensure_state_dir()
    # The "Jarvis Server" scheduled task launches this under pythonw.exe, which
    # has no console — every print() in this codebase (question/answer pairs,
    # Groq errors, rate-limit backoffs) was going nowhere. Diagnosing a live
    # complaint meant guessing instead of reading what actually happened.
    # Redirect stdout/stderr to a plain append-mode log file unconditionally —
    # a manual `python serve.py` run loses live console output, but gets a
    # persisted log instead, which is the more useful trade for a daemon.
    log_f = open(STATE_DIR / "jarvis.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _TimestampedLog(log_f)
    print(f"=== Jarvis starting, version {__import__('common').VERSION} ===", flush=True)
    # Warm the TTS provider so King's first question of the day is not the one
    # that pays for a cold model load — measured once at 34.69s vs a steady
    # ~1.5s once the provider is warm. Backgrounded so it never delays startup.
    threading.Thread(target=listen.say,
                     args=(f"Online, {listen.ADDR}. Do try to contain your excitement.",),
                     daemon=True).start()
    # One live URL for every entry point — the desktop icon, the Start Menu,
    # the taskbar and the "show the HUD" voice command all land here. Derived
    # from the port actually bound, so --port never desyncs the voice command
    # from the server it is meant to open.
    listen.HUD_URL = f"http://127.0.0.1:{args.port}/"

    # Reap zombie segmenters HERE, not just when the ears start: when the
    # pinned mic was absent at boot, the ears never started, so the reaper
    # never ran, and the previous run's ffmpeg lived on holding a dead device
    # (found live 2026-08-15, PID from the prior version still up 20min later).
    try:
        listen._reap_orphan_segmenters()
    except Exception as exc:
        print(f"[jarvis] orphan reap failed: {exc}", flush=True)

    if not args.no_ears:
        if not (load_env_key("GROQ_API_KEY") or listen._watch_env_key("GROQ_API_KEY")):
            print("[jarvis] no GROQ_API_KEY — starting display only", flush=True)
        else:
            def ears_forever():
                """Wait for the microphone rather than giving up at boot.

                The pinned EarPods come and go — unplugged at boot used to mean
                display-only until someone manually restarted the server. Now
                Jarvis polls every 30s and the ears come up on their own within
                half a minute of the earbuds being plugged back in. No fallback
                to other mics: the Intel array reads dead (-84 dB) most days,
                and trusting it is how 'no response' bugs are born. Push-to-talk
                in the HUD (browser mic) keeps working the whole time.
                """
                announced = False
                while True:
                    device = listen.resolve_device(args.device)
                    if device:
                        STATE["ears"], STATE["device"] = True, device
                        print(f"[jarvis] microphone found: {device} — ears on",
                              flush=True)
                        listen.listen_loop(device, args.window, args.gate,
                                           False, args.verbose)
                        return
                    if not announced:
                        print(f"[jarvis] microphone absent "
                              f"(want {args.device or 'any'}) — display only, "
                              f"retrying every 30s", flush=True)
                        announced = True
                    import time
                    time.sleep(30)

            threading.Thread(target=ears_forever, daemon=True).start()

    threading.Thread(target=vault_rescan_loop, daemon=True).start()
    threading.Thread(target=file_reindex_loop, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}/"
    bst = brain.status()
    print(f"\n  JARVIS is live at {url}")
    print(f"  ears: {'ON — ' + STATE['device'] if STATE['ears'] else 'OFF'}")
    print(f"  brain: {bst.get('counts') if bst.get('indexed') else 'NOT INDEXED — run brain.py index'}")
    try:
        import mail
        ms = mail.status()
        print(f"  mail: {ms.get('mailbox') if ms.get('ok') else 'OFFLINE — ' + str(ms.get('error'))[:60]}")
    except Exception as exc:
        print(f"  mail: unavailable ({exc})")
    if args.host == "0.0.0.0":
        print("  ⚠ bound to 0.0.0.0 — every device on this network can read your day")
    print("  ctrl-c to stop\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[jarvis] offline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
