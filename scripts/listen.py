#!/usr/bin/env python3
"""The ears — an always-listening wake-word daemon.

Holds the microphone open, records short windows, drops silent ones locally (so
nothing leaves the machine unless you actually spoke), transcribes the rest with
Groq Whisper, and acts when the text starts with the wake word.

  listen.py                 # start listening
  listen.py --list-devices  # show microphones
  listen.py --once          # capture one window and print the transcript
  listen.py --device "Headset (2- EarPods)"

Intents served locally and instantly:
  brief me / what's my day / good morning   -> speaks ~/.jarvis/brief.json
  top three / what am I working on          -> speaks the objective and steps
  show the HUD / show the display           -> opens the LIVE display (see open_hud)
  stop / quiet / never mind                 -> stops playback
  anything else                             -> queued to ~/.jarvis/inbox.jsonl

Privacy: audio for a window is written to a temp file, uploaded to Groq only if it
clears the loudness gate, and deleted immediately either way. Nothing is retained.
"""
from __future__ import annotations

import argparse
import audioop
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
import webbrowser
from pathlib import Path

from common import (BRIEF_FILE, HUD_FILE, NO_WINDOW, STATE_DIR, ensure_state_dir,
                    load_env_key, read_json)
from config import cfg

# The owner's name and how Jarvis addresses them — "King" / "sir" on the
# original machine, whatever a fresh install's config.json says elsewhere.
# One substitution point (_addr, used by say_and — see below) rather than
# rewriting every one of the ~40 spoken strings in this file: those strings
# stay written as "sir" and get swapped at the last moment before they're
# spoken or returned, so a new owner's name never has to be threaded through
# every f-string by hand.
OWNER_NAME = cfg("owner_name", "King")
ADDR = cfg("owner_address", "sir")


def _addr(text: str) -> str:
    return re.sub(r"\bsir\b", ADDR, text) if ADDR != "sir" else text

SCRIPTS = Path(__file__).resolve().parent
INBOX = STATE_DIR / "inbox.jsonl"
STATUS = STATE_DIR / "listen_status.json"

WAKE = re.compile(r"^\W*(hey\s+|ok\s+|okay\s+)?jarvis\b[\s,.:!-]*", re.I)
# The name, anywhere in the sentence. King says "can you hear me, Jarvis" as
# naturally as "Jarvis, can you hear me" — a prefix-only wake word silently
# dropped the first form, which read as being ignored.
WAKE_ANY = re.compile(r"\b(?:hey\s+|ok\s+|okay\s+)?jarvis\b[\s,.:!-]*", re.I)


def extract_command(text: str) -> str | None:
    """The command in an utterance that names Jarvis, else None.

    The name may sit anywhere: words AFTER it win ("Jarvis, play music" ->
    "play music"); if nothing follows, the words BEFORE it are the command
    ("can you hear me, Jarvis" -> "can you hear me"). No name, no command —
    ambient speech must never trigger him.
    """
    m = WAKE_ANY.search(text or "")
    if not m:
        return None
    after = text[m.end():].strip(" \t,.:;!?-")
    if after:
        return after
    return text[:m.start()].strip(" \t,.:;!?-")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WINDOW = 5          # seconds captured per pass
RMS_GATE = 115      # below this the window is silence and is never uploaded.
                    # Measured on this laptop 2026-08-15: room floor runs 13-72 (it
                    # spikes), and Jarvis's own voice off the speakers hit 104. A
                    # gate of 70 let room noise through and Whisper hallucinated
                    # 'Thank you.' on it. 260 was guessed and ate every command.
                    # Re-measure with miccheck.py --meter; never guess this again.


# --------------------------------------------------------------- audio in

def list_devices() -> list[str]:
    # ffmpeg emits UTF-8; text=True would decode it as cp1252 and turn the Intel
    # array's "®" into "Â®", which then fails to match a real device name.
    r = subprocess.run(["ffmpeg", "-hide_banner", "-list_devices", "true",
                        "-f", "dshow", "-i", "dummy"], capture_output=True, **NO_WINDOW)
    err = (r.stderr or b"").decode("utf-8", "replace")
    return re.findall(r'"([^"]+)"\s*\(audio\)', err)


def capture(device: str, seconds: int, dest: Path) -> bool:
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "dshow",
         "-i", f"audio={device}", "-t", str(seconds),
         "-ac", "1", "-ar", "16000", "-y", str(dest)],
        capture_output=True, text=True, timeout=seconds + 30, **NO_WINDOW)
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 1000


def loudness(path: Path) -> int:
    """Peak RMS over 100ms frames. Peak, not mean — a short phrase inside a mostly
    quiet window averages down to silence and would be thrown away."""
    try:
        with wave.open(str(path), "rb") as w:
            width, rate = w.getsampwidth(), w.getframerate()
            step, peak = int(rate * 0.1), 0
            while True:
                chunk = w.readframes(step)
                if not chunk:
                    return peak
                peak = max(peak, audioop.rms(chunk, width))
    except Exception:
        return 0


# --------------------------------------------------------------- speech to text

def transcribe(path: Path) -> str:
    key = load_env_key("GROQ_API_KEY") or _watch_env_key("GROQ_API_KEY")
    if not key:
        return ""
    b = uuid.uuid4().hex
    body = b""
    # NO `prompt` field. Whisper ECHOES its prompt verbatim when handed silence or
    # noise, and that echo arrives looking exactly like a real command: the prompt
    # used to be "Jarvis, the user's assistant.", so a silent window transcribed as
    # that string, WAKE stripped the "Jarvis," and Jarvis fluently answered "the
    # user's assistant." as though King had asked it. Observed live 2026-08-15.
    # A spelling hint is not worth a phantom command; the wake word is matched
    # case-insensitively anyway.
    for field, val in (("model", "whisper-large-v3-turbo"), ("language", "en"),
                       ("response_format", "text")):
        body += (f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\""
                 f"\r\n\r\n{val}\r\n").encode()
    body += (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode()
    body += path.read_bytes() + f"\r\n--{b}--\r\n".encode()

    # Cloudflare in front of Groq rejects the default python-urllib agent with
    # 403 "error code: 1010" — same block already documented for OpenCode Zen.
    # A normal browser UA passes. This is not an auth failure; do not chase the key.
    req = urllib.request.Request(GROQ_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.read().decode("utf-8", "ignore").strip()
    except urllib.error.HTTPError as e:
        print(f"[whisper] HTTP {e.code}: {e.read().decode('utf-8','ignore')[:160]}",
              file=sys.stderr)
    except Exception as e:
        print(f"[whisper] {e}", file=sys.stderr)
    return ""


# What Whisper emits when handed silence or room noise. These are not things King
# said — they are the model's stock filler, and every one of them was reaching
# handle() as a genuine command. "the user's assistant" is the old prompt echo
# (see transcribe); the rest are Whisper's documented silence artefacts, mostly
# scraped from its YouTube training data.
NOISE_TRANSCRIPTS = {
    "the user's assistant", "thank you", "thanks for watching", "you",
    "thank you for watching", "bye", "bye bye", "okay", "so", ".", "...",
    "please subscribe", "subscribe", "thanks", "i'm sorry", "oh",
}


def is_noise_transcript(text: str) -> bool:
    """True when a transcript is Whisper filler rather than speech.

    Cheap and structural. Without it a silent microphone does not fail loudly —
    it produces a plausible sentence, which the answering model then treats as a
    real question and answers fluently. That is the exact shape of 'it responds
    as though it is not talking to me'.
    """
    t = re.sub(r"[^\w\s']", "", (text or "").strip().lower()).strip()
    return not t or t in NOISE_TRANSCRIPTS


def _watch_env_key(name: str) -> str | None:
    """The /watch skill already stores a Groq key — reuse it rather than making
    King configure the same credential twice."""
    p = Path.home() / ".config" / "watch" / ".env"
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


# --------------------------------------------------------------- speech out

# Set for the whole duration of any spoken reply. Two jobs: it drives the HUD's
# "vault lights up" state, and it deafens the ears so Jarvis never transcribes his
# own voice back into a command.
SPEAKING = threading.Event()

# The HTTP server is threaded, so a typed command and a spoken one can land at once.
# Without this lock two replies talk over each other AND the first to finish clears
# SPEAKING while the second is still speaking — leaving the vault dark mid-sentence.
_MOUTH = threading.Lock()


def say(text: str) -> None:
    """Speak, holding SPEAKING for the full duration of the audio.

    The flag lives here, not in the listen loop, because the HUD's typed box
    reaches handle() without ever touching the loop — putting it upstream is what
    left the vault dark on typed commands.
    """
    with _MOUTH:
        prev = read_json(STATUS, default={}) or {}
        SPEAKING.set()
        write_status(state="responding", device=prev.get("device"), saying=text[:200])
        try:
            subprocess.run([sys.executable, str(SCRIPTS / "speak.py"), text],
                           capture_output=True, text=True, timeout=900, **NO_WINDOW)
        finally:
            SPEAKING.clear()
            write_status(state="listening", device=prev.get("device"),
                         reply=text[:400])


# --------------------------------------------------------------- vault brain

# Answering model, measured on King's own corpus 2026-08-15. gpt-oss-120b was the
# only one of the four Groq chat models that refused to invent: asked something the
# sources did not cover it said "I have nothing on that, sir", where
# llama-3.3-70b CONTINUED the source text and fabricated an email from King to
# prove its point. qwen3.6-27b leaks a <think> block into the spoken reply.
# Do not demote this to llama for cost — both are free on this key.
MODELS = ("openai/gpt-oss-120b", "llama-3.3-70b-versatile")


def _groq(messages: list[dict], max_tokens: int = 320,
          models: tuple[str, ...] = MODELS) -> str:
    """One Groq chat completion, or "" once every model has failed.

    Callers must handle "" — a silent empty string is what keeps a dead key or a
    refusing model from turning into invention.
    """
    key = load_env_key("GROQ_API_KEY") or _watch_env_key("GROQ_API_KEY")
    if not key:
        return ""
    for model in models:
        body = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if "gpt-oss" in model:
            # Without this the model spends its token budget reasoning and returns
            # an empty content field on a 2-sentence butler reply.
            body["reasoning_effort"] = "low"

        # Wait out a rate limit rather than falling straight through. The fallback
        # is a model that CONTINUES its sources instead of answering, so dropping to
        # it on a transient 429 trades a 2-second pause for a wrong answer. Two
        # short retries cover the burst case; a sustained limit still falls through.
        for attempt in range(3):
            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Authorization": f"Bearer {key}",
                         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                         "Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    out = json.loads(r.read().decode("utf-8", "ignore"))
                answer = ((out.get("choices") or [{}])[0].get("message", {})
                          .get("content", "") or "").strip()
                if answer:
                    return answer
                break                                   # empty body: try next model
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    # Groq states the wait in Retry-After; trust it, cap it, so a
                    # spoken question never hangs the butler for a minute.
                    try:
                        wait = float(exc.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        wait = 0
                    wait = min(max(wait, 1.5 * (attempt + 1)), 6.0)
                    print(f"[groq:{model}] 429 — waiting {wait:.1f}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"[groq:{model}] HTTP {exc.code}", file=sys.stderr)
                break
            except Exception as exc:
                print(f"[groq:{model}] {exc}", file=sys.stderr)
                break
    return ""


def _is_bleed(answer: str) -> bool:
    """True when the model copied the SOURCES instead of answering from them.

    Observed live on 2026-08-15: asked what he had decided about MiniMax, the model
    emitted a raw file path followed by a verbatim email header. It is not a
    hallucination — it is continuation — and it looks authoritative, so it has to
    be caught structurally rather than hoped away with prompt wording alone.
    """
    if not answer:
        return False
    a = answer.strip()
    if re.match(r"^[^A-Za-z]", a) and not a.startswith(("QUEUE", "'", '"')):
        return True
    return bool(re.search(r"\[(?:VAULT PAGE|FILE|EMAIL)[: ]|=====\s*(?:BEGIN|END)", a, re.I))


JARVIS_VOICE = (
    f"You are Jarvis — modeled on the MOVIE Jarvis (Tony Stark's AI), not a "
    f"generic assistant. {OWNER_NAME} is your owner; formally polite, address "
    f"them as '{ADDR}' — but EVERY "
    "reply must carry a dry, deadpan, understated sarcastic edge, the way "
    "movie-Jarvis needles Tony. Examples of the register: 'Bold strategy, "
    "sir.' / 'The silence is almost suspicious.' / 'Do try to contain your "
    "excitement.' A reply with no wit in it at all has failed the assignment "
    "— but the wit is delivery, never content: never invent a fact to make a "
    "better joke, and never tip into goofy, punny, or unprofessional. Speak "
    "plainly — never say a filename, variable, JSON key or code identifier "
    "aloud, including ones shown to you in this prompt (say 'your objective', "
    "never 'main_goal'; say 'your goals list', never 'GOALS_FILE'); translate "
    "anything technical into ordinary spoken English first. At most "
    "2 sentences, under 240 characters, no markdown, no emoji, no lists.")

GROUNDED = (
    JARVIS_VOICE + " "
    "Answer ONLY from the SOURCES below. If they do not contain the answer, say "
    "so plainly, with a dry aside about the gap — never fill it from your own "
    "knowledge. Name where it came from in passing ('your vault page on X', "
    "'a file in your Documents', 'an email from Y'). Expand any coined "
    "abbreviation on first use: say 'Merchandise Return case MER-3 PPG Paint "
    "Sprayer', never a bare MER-3.")


def vault_answer(topic: str) -> str:
    """Answer an explicit 'what do you know about X' from the second brain.

    Searches the ObsiVault wiki by CONTENT, not by filename. The old version
    matched a spoken topic against page slugs only, so a question whose answer sat
    in the body of a differently-named page came back as "I have nothing on that".
    """
    import brain
    hits = brain.search(topic, source="vault", k=4)
    if not hits:
        return (f"I have nothing on {topic} in the vault, sir. "
                "Shall I have Claude Code hunt it down properly?")
    blocks = [f"[VAULT PAGE: {h['title']} — {h['path']}]\n"
              f"{brain.passage(h, 1800).strip() or h['snippet']}" for h in hits]
    ctx = "\n\n".join(blocks)[:8000]

    answer = _groq([{"role": "system", "content": GROUNDED},
                    {"role": "user", "content":
                     f"SOURCES:\n{ctx}\n\nQuestion: {topic}\n\n"
                     "(Reminder: dry, deadpan, sarcastic wit, like movie-Jarvis "
                     "needling Tony Stark. A flat factual answer with no edge to "
                     "it has failed the assignment.)"}], 140)
    if answer:
        return answer

    # LLM unavailable -> read the best page itself, clearly labelled as such.
    text = brain.passage(hits[0], 1800)
    lines = [l.strip() for l in text.splitlines()
             if l.strip() and not l.strip().startswith(("#", "|", "-", "["))][:3]
    title = hits[0]["title"]
    return (f"From the vault page on {title}, sir: " + " ".join(lines))[:600] \
        if lines else f"The vault page on {title} has no readable summary, sir."


# --------------------------------------------------------------- web research

GROUNDED_WEB = (
    JARVIS_VOICE + " "
    "You may use up to 4 sentences for a research answer, still under 600 "
    "characters. Answer ONLY from the WEB SOURCES below — they were fetched "
    "seconds ago. Name where facts came from in passing ('per TechCrunch', "
    "'the NHTSA site says'). If the sources do not answer it, say so; never "
    "pad the gap from your own knowledge.")


def web_answer(topic: str) -> str:
    """Research a topic live on the internet and answer with sources."""
    import web
    ctx, hits = web.gather(topic)
    if not ctx:
        return (f"The internet has apparently never heard of {topic}, sir — "
                "or every source refused to load. Shall I have Claude Code dig?")
    answer = _groq([{"role": "system", "content": GROUNDED_WEB},
                    {"role": "user", "content":
                     f"WEB SOURCES:\n{ctx}\n\nQuestion: {topic}\n\n"
                     "(Reminder: dry, deadpan, sarcastic wit, like movie-Jarvis "
                     "needling Tony Stark.)"}], 240)
    if answer and not _is_bleed(answer):
        return answer
    top = hits[0] if hits else {}
    return (f"I found {len(hits)} sources on {topic}, sir — the most promising "
            f"is {top.get('title', 'untitled')} — but my language model is not "
            "cooperating. It is on the display.")


WEB_ASK = re.compile(
    r"\b(?:research|search the web(?: for)?|search online(?: for)?|"
    r"google|check the internet(?: for)?|on the internet|"
    r"what.s the latest(?: news)?(?: on| about)?|latest news (?:on|about))\s*(.*)", re.I)


# --------------------------------------------------------------- email intents

PENDING_EMAIL = STATE_DIR / "pending_email.json"
PENDING_TTL = 300     # a draft King never confirmed dies quietly after 5 min

EMAIL_ASK = re.compile(
    r"^(?:please\s+)?(?:send|write|draft|shoot)\s+(?:an?\s+)?e?-?mail\b(.*)|"
    r"^(?:please\s+)?e-?mail\s+(.+)", re.I)
EMAIL_CONFIRM = re.compile(r"^(?:yes[,.\s]*)?(?:confirm|send it|send the email|"
                           r"go ahead(?: and send(?: it)?)?|fire away)[.!\s]*$", re.I)
EMAIL_CANCEL = re.compile(r"^(?:no[,.\s]*)?(?:cancel|scrap|forget|never ?mind|"
                          r"don.t send)(?:\s+(?:it|that|the e?-?mail))?[.!\s]*$", re.I)


def _draft_email(request: str) -> dict | None:
    """LLM turns the spoken request into {to, subject, body}. None on failure."""
    raw = _groq([
        {"role": "system", "content":
         "Extract an email from the user's spoken request. Reply with ONLY a "
         "JSON object, no prose, no code fences: {\"to\": \"<name or address "
         "as spoken>\", \"subject\": \"<short subject>\", \"body\": \"<the "
         "email, 2-6 sentences, professional but warm, signed 'Darrick "
         "Solomon'>\"}. Write the body from what was asked; do not invent "
         "facts that were not stated."},
        {"role": "user", "content": request}], 400)
    try:
        m = re.search(r"\{.*\}", raw or "", re.S)
        d = json.loads(m.group(0)) if m else None
    except (json.JSONDecodeError, AttributeError):
        d = None
    if not d or not d.get("to") or not d.get("body"):
        return None
    return d


def email_intent(c: str, original: str) -> str | None:
    import mail

    pending = read_json(PENDING_EMAIL, default=None)
    if pending and time.time() - pending.get("at", 0) > PENDING_TTL:
        PENDING_EMAIL.unlink(missing_ok=True)
        pending = None

    if pending and EMAIL_CONFIRM.match(c):
        PENDING_EMAIL.unlink(missing_ok=True)
        r = mail.send(pending["to"], pending["subject"], pending["body"])
        if r.get("ok"):
            return say_and(f"Sent to {pending['to']}, sir. "
                           "Another masterpiece released upon the world.")
        return say_and(f"The send failed, sir: {r.get('error', 'unknown')[:120]}. "
                       "The draft is discarded.")
    if pending and EMAIL_CANCEL.match(c):
        PENDING_EMAIL.unlink(missing_ok=True)
        return say_and("Scrapped, sir. The world will never know what it missed.")

    m = EMAIL_ASK.match(original.strip())
    if not m:
        return None
    request = (m.group(1) or m.group(2) or "").strip()
    if not request:
        return say_and("Email whom about what, sir? Do give me something to work with.")

    draft = _draft_email(original)
    if not draft:
        return say_and("I could not shape that into an email, sir. "
                       "Try naming the recipient and the point.")
    addr, cands = mail.resolve_address(str(draft["to"]))
    if not addr:
        return say_and(f"I have no address for {draft['to']}, sir — nobody by "
                       "that name in your mailbox. Give me the address itself.")
    ensure_state_dir()
    PENDING_EMAIL.write_text(json.dumps({
        "to": addr, "subject": draft.get("subject", "(no subject)"),
        "body": draft["body"], "at": time.time()}, ensure_ascii=False),
        encoding="utf-8")
    gist = re.sub(r"\s+", " ", draft["body"])[:140]
    return say_and(f"Drafted, sir: to {addr}, subject {draft.get('subject')}. "
                   f"It says: {gist}. Say 'send it' to send, or 'cancel'.")


def web_intent(c: str, original: str) -> str | None:
    m = WEB_ASK.search(original)
    if not m:
        return None
    topic = (m.group(1) or "").strip(" ?.!,") or original
    return say_and(web_answer(topic))


# --------------------------------------------------------------- memory intents

REMEMBER_ASK = re.compile(
    r"^(?:please\s+)?(?:remember|make a note|note down|don.t forget)\s*"
    r"(?:that\s+|this[,:]?\s+)?(.+)", re.I)


def remember_intent(c: str, original: str) -> str | None:
    m = REMEMBER_ASK.match(original.strip())
    if not m:
        return None
    fact = m.group(1).strip(" ?.!,")
    if len(fact) < 4:
        return say_and("Remember what, sir? A memory needs contents.")
    import brain
    slug = "-".join(re.findall(r"[a-z0-9]+", fact.lower())[:6]) or "note"
    brain.remember(fact, slug)
    return say_and("Noted and filed, sir. I shall carry it to my grave — "
                   "or the next disk failure, whichever comes first.")


def _auto_capture(command: str) -> None:
    """Canon-style capture: notice a durable personal fact in what King just
    said and file it WITHOUT being asked — he will forget to say 'remember'.

    Runs on a daemon thread after the reply is already out, so it costs him no
    latency. The model is the filter; the prompt is deliberately conservative
    because a memory store full of small talk is worse than none — retrieval
    would serve noise back into every future answer.
    """
    raw = _groq([
        {"role": "system", "content":
         "You maintain a personal assistant's memory of its owner. Decide if "
         "the owner's message contains a DURABLE personal fact worth keeping: "
         "a preference, a person and their relationship, a decision, a standing "
         "instruction, a date that matters. Questions, commands, small talk and "
         "anything already phrased as a request to look something up are NOT "
         "memories. Reply ONLY JSON: {\"save\": true/false, \"fact\": \"<the "
         "fact, one sentence, third person about 'King'>\", \"slug\": "
         "\"<3-5-word-kebab-slug>\"}"},
        {"role": "user", "content": command}], 120)
    try:
        m = re.search(r"\{.*\}", raw or "", re.S)
        d = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return
    if not d or not d.get("save") or not d.get("fact"):
        return
    import brain
    brain.remember(d["fact"], d.get("slug") or "auto-note")
    print(f"[memory] auto-captured: {d.get('slug')}", flush=True)


# --------------------------------------------------------------- intents

def _brief() -> dict:
    return read_json(BRIEF_FILE, default={}) or {}


# --------------------------------------------------------------- the display

# Overridden by serve.py with the port it actually bound.
HUD_URL = "http://127.0.0.1:8787/"

from common import open_in_chrome  # noqa: E402 — CFM-profile-pinned launcher


def _server_alive(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(HUD_URL.rstrip("/") + "/api/health",
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def open_hud() -> str:
    """Open the LIVE display, in Chrome app mode, exactly as the desktop icon does.

    This used to open `~/.jarvis/hud.html` — the static snapshot hud.py writes to
    disk. That file is frozen at whatever the last render produced and, being a
    file:// page, cannot reach the server, so the voice command silently showed an
    older display than the desktop shortcut did. Two Jarvises, one of them stale.
    Everything now points at one live URL.

    Falls back to the snapshot only when the server is genuinely down, and the
    caller says so out loud rather than passing it off as the real thing.
    """
    if _server_alive():
        # Pinned to King's CFM business profile — an unpinned launch opens
        # whichever of his ~28 profiles Chrome used last (King, 2026-08-15:
        # "use my CFM business browser").
        if open_in_chrome(HUD_URL, app=True, window_size="1600,1000"):
            return "chrome"
        webbrowser.open(HUD_URL)
        return "browser"
    if HUD_FILE.exists():
        subprocess.Popen(["cmd", "/c", "start", "", str(HUD_FILE)],
                         shell=False, **NO_WINDOW)
        return "snapshot"
    return "none"


# --------------------------------------------------------------- media intents

PLAY = re.compile(r"^(?:can you |could you |please |go ahead and )?"
                  r"(?:play|put on|queue up|throw on|start playing|fire up)\s+(.+)",
                  re.I)
# Trailing noise a spoken request always carries. Stripped so the YouTube search
# is "kind of blue", not "kind of blue on youtube for me please".
PLAY_TAIL = re.compile(r"\b(?:on|from|via)\s+(?:youtube|yt)\b|\bfor me\b|\bplease\b|"
                       r"\bwould you\b|\bwill you\b", re.I)
MEDIA_NOUN = r"(?:music|song|track|tune|playback|it|that|this|the music)"


def _music_query(raw: str) -> str:
    q = PLAY_TAIL.sub(" ", raw)
    q = re.sub(r"^\s*(?:some|me some|a|an|the)\s+", "", q.strip(), flags=re.I)
    return re.sub(r"\s{2,}", " ", q).strip(" ?.!,")


def music_intent(c: str, original: str) -> str | None:
    """Music commands, checked before the generic stop/quiet handler — otherwise
    'stop the music' is swallowed by it and only kills Jarvis's own voice."""
    import music

    if re.search(rf"\b(?:stop|turn off|kill|shut off)\b.*\b{MEDIA_NOUN}\b", c):
        music.media_key("stop")
        return say_and("Music off, sir. Silence — how refreshing.")
    if re.search(rf"\b(?:pause|hold)\b(?:\s+{MEDIA_NOUN})?$", c) or \
       re.search(rf"\b(?:pause|resume|unpause)\b.*\b{MEDIA_NOUN}\b", c):
        music.media_key("pause")
        return say_and("Paused, sir. The suspense is unbearable.")
    # Anchored: the command must BE a skip request, not merely contain the word
    # "next". The unanchored version heard "look at my schedule for this month
    # and NEXT month" and skipped a nonexistent track instead of answering —
    # live, 2026-08-15 18:07. A word this common must never be a hair trigger.
    if re.fullmatch(rf"(?:please\s+)?(?:next|skip)(?:\s+(?:the\s+)?{MEDIA_NOUN})?\s*[.!]?", c) or \
       re.search(rf"\b(?:next|skip)\b\s+(?:the\s+)?{MEDIA_NOUN}\b", c):
        music.media_key("next")
        return say_and("Next, sir. Onward to marginally better taste.")

    m = PLAY.match(original.strip())
    if not m:
        return None
    # "play the brief" / "play my briefing" is a request to hear the brief, not music.
    if re.search(r"\b(brief|briefing|my day|agenda|schedule)\b", m.group(1), re.I):
        return None
    q = _music_query(m.group(1))
    if not q:
        return None
    r = music.play(q)
    if not r.get("ok"):
        return say_and(f"I could not find anything for {q}, sir. "
                       "Wouldn't be the first thing that didn't exist.")
    return say_and(f"Playing {r['playing']}, sir. "
                   f"{r['queued']} more queued, should your enthusiasm last.")


# --------------------------------------------------------------- mail intents

INBOX_ASK = re.compile(
    r"\b(?:any|new|unread|recent|latest|check|read|catch me up on|"
    r"what.s in)\b[^.?]*\b(?:e-?mails?|mail|inbox|messages)\b|"
    r"\b(?:e-?mail|inbox)\b[^.?]*\b(?:anything|new|today)\b", re.I)


def inbox_intent(c: str) -> str | None:
    if not INBOX_ASK.search(c):
        return None
    import mail
    unread = bool(re.search(r"\bunread\b", c))
    msgs = mail.recent(5, unread_only=unread)
    if not msgs:
        st = mail.status()
        if not st.get("ok"):
            return say_and("I cannot reach your mailbox, sir — "
                           "the Google authorisation has lapsed again.")
        return say_and("Nothing new in the inbox, sir. The silence is almost suspicious.")
    head = f"{len(msgs)} in your inbox, sir. " if not unread else \
           f"{len(msgs)} unread, sir. "
    return say_and(head + " ".join(mail.line(m) + "." for m in msgs))


# --------------------------------------------------------------- file intents

FIND_FILE = re.compile(
    r"\b(?:where(?:'s| is| are)|find|locate|dig up|look for)\b[^.?]*?"
    r"\b(file|folder|documents?|docs?|pdf|spreadsheets?|sheet|screenshots?|"
    r"photos?|pictures?|images?|videos?|scripts?|notes?|downloads?)\b"
    r"\s*(?:called|named|about|for|on|regarding)?\s*(.*)", re.I)

# "find my document about X" should not return an .mp3 that happens to be named X.
# The noun King used is a type filter, so honour it.
KIND_EXT = {
    "document": {".md", ".txt", ".doc", ".docx", ".pdf", ".rtf", ".odt"},
    "doc": {".md", ".txt", ".doc", ".docx", ".pdf", ".rtf", ".odt"},
    "note": {".md", ".txt"},
    "pdf": {".pdf"},
    "spreadsheet": {".csv", ".tsv", ".xls", ".xlsx", ".ods"},
    "sheet": {".csv", ".tsv", ".xls", ".xlsx", ".ods"},
    "screenshot": {".png", ".jpg", ".jpeg", ".webp", ".bmp"},
    "photo": {".png", ".jpg", ".jpeg", ".webp", ".heic", ".bmp"},
    "picture": {".png", ".jpg", ".jpeg", ".webp", ".heic", ".bmp"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".heic", ".bmp", ".svg", ".gif"},
    "video": {".mp4", ".mov", ".mkv", ".webm", ".avi"},
    "script": {".py", ".js", ".ts", ".ps1", ".sh", ".bat", ".cmd", ".rb", ".go"},
}


def _kind_filter(noun: str) -> set[str] | None:
    return KIND_EXT.get(re.sub(r"s$", "", (noun or "").lower().strip()))
OPEN_FILE = re.compile(r"^(?:open|pull up|bring up)\s+(?:the\s+|my\s+)?"
                       r"(?:file|document|doc|folder|pdf|spreadsheet)?\s*(.+)", re.I)


def _where(path: str) -> str:
    """Spoken location. The full path read aloud is unusable; the parent folder is
    what King actually needs to go and look."""
    p = Path(path)
    return p.parent.name or str(p.parent)


def file_intent(c: str, original: str) -> str | None:
    import brain

    m = FIND_FILE.search(original)
    if m:
        noun, topic = m.group(1), m.group(2).strip(" ?.!,") or original
        hits = brain.search(topic, source="file", k=40)
        exts = _kind_filter(noun)
        if exts:
            narrowed = [h for h in hits if Path(h["path"]).suffix.lower() in exts]
            # An empty narrowing means the type filter was wrong, not that the file
            # is absent — fall back rather than report nothing.
            hits = narrowed or hits
        if not hits:
            return say_and(f"Nothing on this machine matches {topic}, sir. "
                           "Have you considered it never existed?")
        # Same basename in three folders read aloud as "X and X and X". Collapse to
        # one entry per filename, and always say the folder — the name alone is not
        # an answer to "where is it".
        seen, picks = set(), []
        for h in hits:
            nm = Path(h["path"]).name.lower()
            if nm in seen:
                continue
            seen.add(nm)
            picks.append(h)
            if len(picks) == 3:
                break
        parts = [f"{Path(h['path']).name} in {_where(h['path'])}" for h in picks]
        out = f"{parts[0]}, sir."
        if parts[1:]:
            out += " Then " + ", then ".join(parts[1:]) + "."
        return say_and(out)

    m = OPEN_FILE.match(original.strip())
    if m and not re.search(r"\b(hud|display|dashboard|calendar|browser|email|inbox)\b",
                           m.group(1), re.I):
        topic = m.group(1).strip(" ?.!,")
        hits = brain.search(topic, source="file", k=1)
        if not hits:
            return None                     # let converse() try — it may be a topic
        target = Path(hits[0]["path"])
        try:
            os.startfile(str(target))       # noqa: S606 — user-initiated, local file
        except Exception as exc:
            return say_and(f"I could not open that, sir: {exc}")
        return say_and(f"Opening {target.name}, sir, from {_where(str(target))}.")
    return None


def handle(command: str) -> str:
    """Return what Jarvis said back."""
    c = command.lower().strip()
    b = _brief()

    if not c or re.fullmatch(r"[\W_]*", c):
        return say_and("Yes, sir? Do go on.")

    # Mic check gets an INSTANT canned reply — no retrieval, no LLM. "Can you
    # hear me" is a man testing a wire; making him wait on a language model
    # round-trip (or worse, hearing about what the vault says on 'hearing')
    # defeats the entire point of the question.
    if re.search(r"\b(?:can|do)\s+you\s+hear\s+me\b|\bare\s+you\s+(?:listening|there)\b|"
                 r"\bmic\s*(?:check|test)\b|\btesting[ ,]+testing\b|\bhear\s+me\s+now\b", c):
        return say_and("Loud and clear, sir.")

    for intent in (lambda: remember_intent(c, command),
                   lambda: email_intent(c, command),
                   lambda: music_intent(c, command),
                   lambda: inbox_intent(c),
                   lambda: file_intent(c, command),
                   lambda: web_intent(c, command)):
        try:
            got = intent()
        except Exception as exc:            # a broken capability must not eat the turn
            print(f"[jarvis] intent error: {exc}", file=sys.stderr)
            got = None
        if got is not None:
            return got

    if re.search(r"\b(stop|quiet|silence|never mind|nevermind|cancel)\b", c):
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-Process -Name wmplayer,powershell_ise -EA SilentlyContinue "
                        "| Stop-Process -Force -EA SilentlyContinue"],
                       capture_output=True, **NO_WINDOW)
        return "(stopped)"

    if re.search(r"\b(hud|display|screen|dashboard)\b", c):
        how = open_hud()
        if how == "snapshot":
            return say_and("The display is up, sir, but only a snapshot — "
                           "the server appears to be sulking.")
        if how == "none":
            return say_and("There is no display rendered yet, sir. "
                           "I can only work with what I'm given.")
        return say_and("Display is up, sir. Do try to look impressed.")

    if re.search(r"\b(top three|top 3|objective|working on|priorit)\b", c):
        goal = b.get("main_goal")
        if not goal:
            return say_and("You have not set an objective, sir. Bold strategy.")
        steps = [s.get("text") if isinstance(s, dict) else s for s in (b.get("steps") or [])]
        return say_and(f"Your objective is {goal}. " +
                       " ".join(f"{i}. {s}." for i, s in enumerate(steps, 1)))

    # Only a SHORT, direct ask gets the canned brief. "Look at my schedule for
    # this month and next month and give me a summation" contains the word
    # "schedule" but is real work — the unguarded keyword match read him the
    # morning brief instead of doing what he asked (live, 2026-08-15). Length
    # is the cheap honest test: nobody asks for the brief in forty words.
    if len(c) <= 40 and \
       re.search(r"\b(brief|briefing|my day|good morning|what.s (my|the) day|agenda|schedule)\b", c):
        spoken = b.get("spoken")
        if not spoken:
            return say_and("I have no briefing prepared, sir. Even I need something to work with.")
        return say_and(spoken)

    # Explicit vault lookups only. A bare "what is ..." is ordinary conversation and
    # must fall through to converse(), or every question gets answered with
    # "I have nothing on that in the vault."
    m = re.search(r"(?:what do you know about|tell me about|look (?:it )?up|"
                  r"search (?:the )?vault(?: for)?|check the vault(?: for)?|"
                  r"(?:what|who) does the vault say about|"
                  r"(?:pull up|read me) the (?:page|wiki|vault page) (?:on|for|about))"
                  r"\s+(.+)", c)
    if m:
        return say_and(vault_answer(m.group(1).strip(" ?.!")))

    # Anything else: converse. The LLM answers what it can from context and flags
    # real work with QUEUE:, which lands in the inbox for Claude Code.
    return converse(command)


DIALOG = STATE_DIR / "dialog.json"


def _queue_for_code(command: str) -> None:
    ensure_state_dir()
    with INBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _dt.datetime.now().isoformat(timespec="seconds"),
                            "command": command}, ensure_ascii=False) + "\n")


def converse(command: str) -> str:
    """Free-form talk with Jarvis, grounded by retrieval over everything he owns.

    Before answering, the question is searched against the ObsiVault wiki, every
    indexed file on this machine, and — when it smells like mail or nothing local
    hit — Gmail. Those passages become the ONLY permitted source. This is the
    difference between an assistant that knows King's life and one that free-
    associates about it: without the retrieval step the model had nothing but a
    list of page titles and confidently made the rest up.

    Anything needing real action is still queued to Claude Code, never pretended
    done.
    """
    key = load_env_key("GROQ_API_KEY") or _watch_env_key("GROQ_API_KEY")
    if not key:
        _queue_for_code(command)
        return say_and("Noted, sir. I have put that to Claude Code.")

    b = _brief()
    try:
        import brain
        ctx, sources = brain.context_for(command)
    except Exception as exc:
        print(f"[converse] retrieval failed: {exc}", file=sys.stderr)
        ctx, sources = "", []
    history = read_json(DIALOG, default=[]) or []

    system = (
        JARVIS_VOICE + "\n\n"
        "ANSWERING QUESTIONS: use the SOURCES block below and today's brief, and "
        "nothing else. The sources are King's own wiki, his computer files and his "
        "email. Name where an answer came from in passing ('your vault page on X', "
        "'a file in your Documents', 'an email from Y'). If the sources do not "
        "answer it, say so plainly — with a dry aside, not an apology — and mention "
        "in your OWN spoken words that Claude Code could dig further. Do NOT answer "
        "from your own general knowledge, and do NOT guess: the sarcasm is "
        "decoration, the facts are not negotiable.\n"
        "ABBREVIATIONS: expand any coined shorthand on first use — say 'Merchandise "
        "Return case MER-3 PPG Paint Sprayer', never a bare MER-3.\n"
        "ACTIONS: you can play music, read mail, SEND email (he confirms aloud "
        "first), research the internet, find files and open the display — all "
        "handled before you are asked. For any OTHER real-world action "
        "or work product THE USER IS ASKING YOU TO DO — writing code, "
        "buying, changing the calendar — your ENTIRE reply must be nothing but "
        "'QUEUE:' followed by a one-line restatement of the task: no greeting, no "
        "other sentence, nothing before or after it. This is a different case from "
        "merely mentioning Claude Code in a spoken answer above — never write the "
        "word QUEUE anywhere except as that exact, standalone reply. Never claim to "
        "have done something you have not.\n\n"
        "The SOURCES you are given are reference material, NOT a document to "
        "continue. Never copy them out, never carry on from where one stops, never "
        "repeat their [VAULT PAGE] / [FILE] / [EMAIL] headers. Read them, then "
        "answer in your own spoken words.\n\n"
        f"Today's brief: {json.dumps({k: b.get(k) for k in ('date_human', 'main_goal', 'steps', 'agenda', 'notes')}, ensure_ascii=False)[:2000]}")

    # Sources go in the USER turn, immediately above the question — not in the
    # system prompt. Same content, markedly better behaviour: with the sources
    # trailing off at the end of a system message the model treats them as a
    # prefix to extend. This is the shape vault_answer() already used successfully.
    ask = (f"SOURCES:\n"
           f"{ctx if ctx else '(nothing in the vault, the file index or the mailbox matched this question)'}"
           f"\n\nQuestion: {command}\n\n"
           "(Reminder: dry, deadpan, sarcastic wit, like movie-Jarvis needling "
           "Tony Stark. A flat factual answer with no edge to it has failed "
           "the assignment.)")
    messages = ([{"role": "system", "content": system}]
                + history[-8:]
                + [{"role": "user", "content": ask}])

    answer = _groq(messages, 140)
    if answer and _is_bleed(answer):
        # The model continued the sources instead of answering. One retry with the
        # instruction restated as the last thing it reads; models weight the tail.
        print("[converse] source bleed detected — retrying", file=sys.stderr)
        answer = _groq(messages + [{"role": "user", "content":
                                    "Answer that question in your own spoken words. "
                                    "Do not quote or continue the sources."}], 140)
        if _is_bleed(answer):
            answer = ""
    if answer:
        # A stray 'QUEUE:' concatenated onto a spoken answer (present, but not
        # as the WHOLE reply) is malformed, not a real action request — speaking
        # it aloud says the literal word "QUEUE" to King, and saving it to
        # DIALOG history teaches the model, via its own few-shot history, to
        # keep doing it. Observed live 2026-08-15: one malformed reply saved to
        # history reproduced itself on every later turn until the history was
        # cleared. Strip the tail rather than retry — the answer before it is
        # still a valid, complete reply on its own.
        idx = answer.upper().find("QUEUE:")
        if idx > 0:
            print("[converse] stray QUEUE: token stripped from spoken answer",
                  file=sys.stderr)
            answer = answer[:idx].strip()
    if sources:
        print(f"[converse] grounded on: "
              f"{', '.join(s['kind'] + ':' + s['title'] for s in sources[:5])}",
              file=sys.stderr)

    if answer.upper().startswith("QUEUE:"):
        task = answer[6:].strip() or command
        _queue_for_code(task)
        return say_and("Very good, sir. I have put that to Claude Code — "
                       "it will be picked up next session.")
    if not answer:
        _queue_for_code(command)
        return say_and("I could not reach my language model, sir. "
                       "I have queued that for Claude Code instead.")

    history = (history + [{"role": "user", "content": command},
                          {"role": "assistant", "content": answer}])[-12:]
    ensure_state_dir()
    DIALOG.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    # Canon-style auto-capture, off the reply path — durable facts King mentions
    # in passing get filed even when he never says "remember".
    threading.Thread(target=_auto_capture, args=(command,), daemon=True).start()
    return say_and(answer)


def say_and(text: str) -> str:
    """Fire the reply audio in the background and return the text at once.

    say() used to run inline, so POST /api/say did not return until the audio
    finished playing — King watched "Working…" for the full 16-50s of speech
    when the answer had existed since second one. _MOUTH and SPEAKING still
    serialise playback (see say()), so a second reply queues instead of
    overlapping; only the blocking wait on THIS reply is removed.

    Also the one substitution point for the configured address term (see
    _addr) — every canned string in this file is written as "sir" and gets
    swapped here, once, so the spoken audio, the HUD text and the DIALOG
    history King's memory reads back all agree with each other.
    """
    text = _addr(text)
    threading.Thread(target=say, args=(text,), daemon=True).start()
    return text


# --------------------------------------------------------------- loop

def write_status(**kw) -> None:
    ensure_state_dir()
    kw["at"] = _dt.datetime.now().isoformat(timespec="seconds")
    STATUS.write_text(json.dumps(kw, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default=None, help="microphone name (default: first found)")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--once", action="store_true", help="one window, print transcript, exit")
    p.add_argument("--window", type=int, default=WINDOW)
    p.add_argument("--gate", type=int, default=RMS_GATE)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    devices = list_devices()
    if args.list_devices:
        print(json.dumps({"devices": devices}, indent=2))
        return 0
    if not devices:
        print("no audio input devices found", file=sys.stderr)
        return 2
    if args.device:
        # Accept a substring so callers never have to reproduce "Intel®" exactly —
        # a .cmd launcher in cp437 cannot carry that character.
        match = [d for d in devices if args.device.lower() in d.lower()]
        if not match:
            print(f"device not found: {args.device}\navailable: {devices}", file=sys.stderr)
            return 2
        device = match[0]
    else:
        # Built-in array beats a headset that may be unplugged.
        builtin = [d for d in devices if "microphone array" in d.lower()]
        device = builtin[0] if builtin else devices[0]

    key_ok = bool(load_env_key("GROQ_API_KEY") or _watch_env_key("GROQ_API_KEY"))
    if not key_ok:
        print("no GROQ_API_KEY — set it in ~/.jarvis/.env", file=sys.stderr)
        return 3

    return listen_loop(device, args.window, args.gate, args.once, args.verbose)


DEAD_DB = -55.0     # a mic whose loudest sample never beats this is not working


def device_level(device: str, seconds: float = 1.5) -> float:
    """Peak dBFS the device sees. ffmpeg's volumedetect is the honest measure —
    a muted or disabled mic still 'captures' happily, it just returns silence."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "dshow", "-i", f"audio={device}",
         "-t", str(seconds), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, timeout=int(seconds) + 40, **NO_WINDOW)
    err = (r.stderr or b"").decode("utf-8", "replace")
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", err)
    return float(m.group(1)) if m else -99.0


def resolve_device(want: str | None) -> str | None:
    """Pick a microphone that actually hears something.

    Do NOT prefer the built-in array on name alone: on this laptop the Intel array
    is dead (max -61 dB) while the EarPods headset works, and preferring the array
    left Jarvis unable to hear anything at all. Measure, then choose.
    """
    devices = list_devices()
    if not devices:
        return None
    if want:
        m = [d for d in devices if want.lower() in d.lower()]
        return m[0] if m else None

    scored = []
    for d in devices:
        try:
            lvl = device_level(d)
        except Exception:
            lvl = -99.0
        scored.append((lvl, d))
        print(f"[jarvis] mic probe: {lvl:>7.1f} dB  {d}", flush=True)

    scored.sort(reverse=True)
    best_lvl, best = scored[0]
    if best_lvl < DEAD_DB:
        print(f"[jarvis] WARNING: every microphone reads below {DEAD_DB} dB — "
              f"Jarvis will not hear you. Check Windows mic levels.", flush=True)
    return best


# ------------------------------------------------------- utterance assembly
#
# The old ears captured fixed 5-second windows. Two structural failures came
# straight from that shape, both observed live on 2026-08-15:
#   * a phrase spanning a window boundary is split — neither half carries the
#     wake word, so the whole utterance is silently dropped ("no response");
#   * the reply cannot begin until the window closes, so even a heard command
#     waits out the remainder of a 5s window before transcription starts.
# The fix is to capture CONTINUOUSLY in small segments and cut an utterance
# where the speech actually ends, not where a timer happens to fire.

SEG_SECONDS = 0.6      # one segment ≈ one syllable-group; boundary loss ~0
END_QUIET_SEGS = 2     # ~1.2s of quiet after speech = the sentence is over
MAX_UTTER_SEGS = 20    # ~12s hard cap so a droning TV can't build a monster
PREROLL_SEGS = 1       # keep one quiet segment before onset: word attacks live there


class UtteranceAssembler:
    """Feed (rms, path) per segment; returns a list of segment paths exactly
    once per finished utterance, else None.

    The speech threshold adapts: quiet segments feed a rolling noise floor and
    speech must clear max(gate, 3x floor). This mic reported ambient RMS 787
    one minute and 2 the next — any fixed gate is wrong half the time on it.
    Pure logic, no I/O, so it is unit-testable without a microphone.
    """

    WARMUP_SEGS = 3        # first ~1.8s calibrates the floor, never counts as speech

    def __init__(self, gate: int = RMS_GATE):
        self.gate = gate
        self.floor: list[int] = []
        self.preroll: list[str] = []
        self.collecting: list[tuple[int, str]] = []
        self.quiet = 0
        self.warmup = self.WARMUP_SEGS
        self.last_cut_rms = 0

    def threshold(self) -> int:
        if not self.floor:
            return self.gate
        med = sorted(self.floor)[len(self.floor) // 2]
        return max(self.gate, med * 3)

    def learn_noise(self, rms: int) -> None:
        """Teach the floor that a level was NOISE after all. Called when a cut
        'utterance' transcribed to nothing or Whisper filler — without this, a
        mic that flips to hot static mid-session re-triggers forever; with it,
        one junk upload is the entire cost of learning the new floor.

        RESEEDS rather than appends: one sample cannot outvote a median built
        from forty samples of the mic's OLD personality, and this device does
        change personality (measured RMS 2 and 787 minutes apart). If the spike
        was a one-off, genuinely quiet segments re-fill the window and the
        median walks back down within a few seconds — self-correcting."""
        self.floor = [max(rms, self.gate)] * 8

    def _floor_add(self, rms: int) -> None:
        self.floor.append(rms)
        if len(self.floor) > 40:
            self.floor.pop(0)

    def feed(self, rms: int, path: str) -> list[str] | None:
        if self.warmup > 0:
            # Cold start: no baseline exists yet, so ANY level would clear a
            # bare gate — a mic that boots into static would read as speech
            # from segment one. Calibrate first; 1.8s of deafness at boot.
            self.warmup -= 1
            self._floor_add(rms)
            self.preroll = [path]
            return None
        if rms >= self.threshold():
            if not self.collecting:
                self.collecting = [(0, p) for p in self.preroll]  # word's attack
            self.collecting.append((rms, path))
            self.quiet = 0
            if len(self.collecting) >= MAX_UTTER_SEGS:
                return self._cut()
            return None
        # quiet segment
        self._floor_add(rms)
        self.preroll = [path]
        if not self.collecting:
            return None
        self.quiet += 1
        self.collecting.append((rms, path))              # trailing decay
        if self.quiet >= END_QUIET_SEGS:
            return self._cut()
        return None

    def _cut(self) -> list[str]:
        loud = sorted(r for r, _ in self.collecting if r > 0)
        self.last_cut_rms = loud[len(loud) // 2] if loud else 0
        out = [p for _, p in self.collecting]
        self.collecting, self.quiet = [], 0
        return out


def _concat_wavs(paths: list[str], dest: Path) -> bool:
    """Stitch same-format segments into one clip Whisper hears as a sentence.

    Per-file try: one truncated or vanished segment must cost ITSELF, not the
    whole utterance — observed live as 'concat failed: # channels not
    specified' eating a complete sentence because its preroll was unreadable.
    """
    wrote = False
    try:
        with wave.open(str(dest), "wb") as out:
            for p in paths:
                try:
                    with wave.open(p, "rb") as w:
                        params = w.getparams()
                        if params.nchannels < 1:
                            continue
                        if not wrote:
                            out.setparams(params)
                            wrote = True
                        out.writeframes(w.readframes(w.getnframes()))
                except Exception:
                    continue                     # skip the bad segment, keep the sentence
        return wrote and dest.exists() and dest.stat().st_size > 1000
    except Exception as exc:
        print(f"[jarvis] concat failed: {exc}", file=sys.stderr)
        return False


def _reap_orphan_segmenters(keep_dir: Path | None = None) -> None:
    """Kill segmenters left over from a previous run, and their litter.

    A restart via Stop-ScheduledTask hard-kills pythonw, so our finally-block
    never runs and the old ffmpeg SURVIVES — holding the microphone open and
    writing segments into a directory nobody consumes (found live 2026-08-15:
    322 files and climbing, plus mic contention with the new run). Matching on
    the jarvis_ears_ path in the command line keeps this surgical: ffmpeg
    doing anything else on the machine is untouched.
    """
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
         "Where-Object { $_.CommandLine -match 'jarvis_ears_' } | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
         "-ErrorAction SilentlyContinue }"],
        capture_output=True, timeout=30, **NO_WINDOW)
    import shutil
    base = Path(tempfile.gettempdir())
    for d in base.glob("jarvis_ears_*"):
        if keep_dir is None or d != keep_dir:
            shutil.rmtree(d, ignore_errors=True)


def _spawn_segmenter(device: str, seg_dir: Path) -> subprocess.Popen:
    """One long-lived ffmpeg writing 0.6s wav segments. One process instead of
    one per window also ends the constant open/close churn on a flaky device."""
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "dshow",
         "-i", f"audio={device}", "-ac", "1", "-ar", "16000",
         "-f", "segment", "-segment_time", str(SEG_SECONDS),
         "-reset_timestamps", "1", str(seg_dir / "seg%06d.wav")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NO_WINDOW)


def _handle_utterance(text: str, device: str) -> None:
    """Everything after Whisper: log ALWAYS, then decide. The old loop only
    printed in --verbose, so 'Jarvis never answered' left no trace to read."""
    print(f"[jarvis] heard: {text!r}", flush=True)
    if is_noise_transcript(text):
        print("[jarvis]   .. filler, dropped", flush=True)
        return
    command = extract_command(text)
    if command is None:
        # No wake word — not for him. Logged and surfaced so the HUD can show
        # WHY there was no reply, but Jarvis stays quiet: answering ambient
        # speech is how an assistant becomes an eavesdropper.
        print("[jarvis]   .. no wake word, ignored", flush=True)
        write_status(state="listening", device=device,
                     heard=f"{text} (no wake word — say 'Jarvis')")
        return
    if is_noise_transcript(command):
        print("[jarvis] > (wake only, no command)", flush=True)
        say(_addr("I caught my name but not the question, sir. Try me again."))
        return
    print(f"[jarvis] > {command}", flush=True)
    write_status(state="responding", device=device, heard=command)
    try:
        reply = handle(command)
        print(f"[jarvis] < {reply}", flush=True)
        write_status(state="listening", device=device,
                     heard=command, reply=reply[:400])
    except Exception as exc:                 # one bad turn must not kill the ears
        print(f"[jarvis] handler error: {exc}", file=sys.stderr, flush=True)
        write_status(state="listening", device=device, error=str(exc)[:200])


def listen_loop(device: str, window: int = WINDOW, gate: int = RMS_GATE,
                once: bool = False, verbose: bool = False) -> int:
    """The ears. Blocking; run it in a thread when the HTTP server owns the process."""
    ensure_state_dir()
    if once:
        # Diagnostic mode keeps the simple one-window shape: capture, print, exit.
        tmp = Path(tempfile.gettempdir()) / f"jarvis_once_{uuid.uuid4().hex[:6]}.wav"
        if not capture(device, window, tmp):
            print(json.dumps({"error": "capture failed"}))
            return 2
        rms = loudness(tmp)
        text = transcribe(tmp) if rms >= gate else ""
        tmp.unlink(missing_ok=True)
        print(json.dumps({"rms": rms, "gate": gate, "heard": text}))
        return 0

    seg_dir = Path(tempfile.mkdtemp(prefix="jarvis_ears_"))
    _reap_orphan_segmenters(keep_dir=seg_dir)
    proc = _spawn_segmenter(device, seg_dir)
    asm = UtteranceAssembler(gate)
    consumed = 0
    pending: set[str] = set()   # consumed segments awaiting deletion — a replaced
                                # preroll file leaks ~100 files/min if only the
                                # current segment is ever considered for cleanup
    print(f"[jarvis] listening on: {device} (segmented, {SEG_SECONDS}s)", flush=True)
    write_status(state="listening", device=device)
    try:
        while True:
            if proc.poll() is not None:
                # Device unplugged or driver hiccup. Say so loudly and retake
                # whatever microphone still works rather than going quietly deaf.
                print("[jarvis] segmenter died — restarting ears", flush=True)
                write_status(state="capture_failed", device=device)
                time.sleep(2)
                device = resolve_device(None) or device
                proc = _spawn_segmenter(device, seg_dir)
                asm = UtteranceAssembler(gate)
                write_status(state="listening", device=device)
                continue

            # A segment is complete once a higher-numbered file exists.
            segs = sorted(seg_dir.glob("seg*.wav"))
            ready = [s for s in segs[:-1]
                     if int(s.stem[3:]) >= consumed] if len(segs) > 1 else []
            if not ready:
                time.sleep(0.15)
                continue

            for seg in ready:
                consumed = int(seg.stem[3:]) + 1
                if SPEAKING.is_set():
                    # Deaf while talking, or he transcribes his own voice.
                    asm.collecting, asm.quiet = [], 0
                    seg.unlink(missing_ok=True)
                    continue
                utter = asm.feed(loudness(seg), str(seg))
                pending.add(str(seg))
                # Sweep everything consumed-but-no-longer-held, not just the
                # current segment: a preroll file replaced by a newer one was
                # otherwise never revisited and leaked to disk forever.
                held = {p for _, p in asm.collecting} | set(asm.preroll)
                for p in [p for p in pending if p not in held]:
                    if not utter or p not in utter:
                        Path(p).unlink(missing_ok=True)
                        pending.discard(p)
                if not utter:
                    continue

                clip = seg_dir / f"utter_{uuid.uuid4().hex[:6]}.wav"
                ok = _concat_wavs(utter, clip)
                for p in utter:
                    Path(p).unlink(missing_ok=True)
                    pending.discard(p)
                if not ok:
                    continue
                text = transcribe(clip)
                clip.unlink(missing_ok=True)
                if not text or is_noise_transcript(text):
                    # It cleared the level threshold but carried no speech —
                    # a hot-static mic, a fan spinning up. Teach the floor so
                    # the same level stops re-triggering forever.
                    asm.learn_noise(asm.last_cut_rms)
                if text:
                    _handle_utterance(text, device)
    except KeyboardInterrupt:
        write_status(state="stopped", device=device)
        print("\n[jarvis] ears closed.")
        return 0
    finally:
        try:
            proc.kill()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
