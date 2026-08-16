#!/usr/bin/env python3
"""JARVIS INSTALLER — sets up a personal Jarvis on THIS computer.

Run this from inside a copy of the jarvis skill folder (the one that contains
scripts/, requirements.txt, and this product/ directory) — i.e. after copying
the whole `jarvis` folder to ~/.claude/skills/jarvis on the new computer.

  python product/install_jarvis.py

What it does, in order, printing PASS/FAIL for each step so nothing is a
silent assumption:
  1. Verify the skill folder is actually in place (fails loud if not — this
     script does not copy itself, the human/agent running it does that first)
  2. Install Python dependencies from requirements.txt
  3. Interview: name, how Jarvis should address you, ObsiVault location,
     Chrome profile, microphone, DeepInfra key, Groq key
  4. Write ~/.jarvis/config.json + ~/.jarvis/.env  (per-machine, never the
     original owner's files — see scripts/config.py)
  5. Create a starter ObsiVault (wiki/ + index.md + log.md + hot.md +
     CLAUDE.md) if one doesn't already exist at the chosen path
  6. Build the FTS5 brain index for the first time
  7. Generate app icons
  8. Create the "Jarvis Server" scheduled task (at-logon, auto-restart)
  9. Create Desktop + Start Menu shortcuts, pinned to the chosen Chrome profile
  10. Print the phone-install address

Google Gmail (read + send) is NOT wired here — it needs its own OAuth client
and is a separate, explicit step (see the printed note at the end). Everything
else works without it.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
STATE_DIR = Path.home() / ".jarvis"

PASS, FAIL, INFO = "\033[92m[PASS]\033[0m", "\033[91m[FAIL]\033[0m", "\033[96m[ .. ]\033[0m"


def step(label: str):
    print(f"\n{INFO} {label}")


def ok(msg: str):
    print(f"{PASS} {msg}")


def bad(msg: str) -> None:
    print(f"{FAIL} {msg}")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {prompt}{suffix}: ").strip()
    return val or default


# --------------------------------------------------------------- 1. verify layout

def verify_layout() -> None:
    step("Checking this is a real jarvis skill folder")
    missing = [p for p in ("scripts/brain.py", "scripts/serve.py", "scripts/config.py",
                           "requirements.txt")
               if not (SKILL_DIR / p).exists()]
    if missing:
        bad(f"missing: {missing}")
        print(f"\n  This script expects to live inside a full copy of the jarvis skill\n"
              f"  folder (scripts/, requirements.txt, product/). Copy the WHOLE folder\n"
              f"  first, then run this script from inside it.")
        raise SystemExit(2)
    ok(f"skill folder complete at {SKILL_DIR}")


# --------------------------------------------------------------- 2. dependencies

def install_deps() -> None:
    step("Installing Python dependencies")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                        "-r", str(SKILL_DIR / "requirements.txt")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        bad("pip install failed:\n" + r.stderr[-800:])
        raise SystemExit(2)
    ok("dependencies installed")


# --------------------------------------------------------------- 3. interview

def list_chrome_profiles() -> list[tuple[str, str]]:
    """[(profile_dir, label)] — read straight from Chrome's own state, the
    same way King's own profile was identified, so this works on ANY
    Windows account without guessing."""
    base = Path.home() / "AppData/Local/Google/Chrome/User Data"
    local_state = base / "Local State"
    out = []
    try:
        info = json.loads(local_state.read_text(encoding="utf-8"))["profile"]["info_cache"]
        for d, meta in info.items():
            name = meta.get("name", d)
            email = meta.get("user_name", "")
            out.append((d, f"{name}" + (f" ({email})" if email else "")))
    except Exception:
        pass
    return out


def list_mics() -> list[str]:
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"],
                           capture_output=True, timeout=15)
        err = (r.stderr or b"").decode("utf-8", "replace")
        return re.findall(r'"([^"]+)"\s*\(audio\)', err)
    except Exception:
        return []


def interview() -> dict:
    step("A few questions — press Enter to accept the [default]")
    name = ask("Your first name", "Friend")
    addr = ask('How should Jarvis address you? ("sir", "ma\'am", your name, "boss"...)',
               "boss")

    default_vault = str(Path.home() / "Documents" / f"{name}'s Brain" / "ObsiVault")
    vault = ask("Where should your second-brain vault live", default_vault)

    profiles = list_chrome_profiles()
    if profiles:
        print("  Chrome profiles found:")
        for i, (d, label) in enumerate(profiles):
            print(f"    {i}: {label}  [{d}]")
        pick = ask(f"Which one is yours (0-{len(profiles)-1})", "0")
        try:
            chrome_profile = profiles[int(pick)][0]
        except (ValueError, IndexError):
            chrome_profile = "Default"
    else:
        chrome_profile = ask("Chrome profile folder name (e.g. 'Default')", "Default")

    mics = list_mics()
    mic = ""
    if mics:
        print("  Microphones found:")
        for i, m in enumerate(mics):
            print(f"    {i}: {m}")
        pick = ask("Which one do you talk into (blank = auto-pick the loudest)", "")
        if pick.strip().isdigit() and int(pick) < len(mics):
            mic = mics[int(pick)]

    deepinfra = ask("Your DeepInfra API key (voice)")
    groq = ask("Your Groq API key (speech-to-text + answering model, free at "
               "console.groq.com/keys)")

    return {"owner_name": name, "owner_address": addr, "vault_dir": vault,
            "hot_md": str(Path(vault) / "hot.md"),
            "extra_env_files": [], "memory_dirs": [], "extra_google_tokens": [],
            "chrome_profile": chrome_profile, "mic_device": mic, "port": 8787,
            "_deepinfra_key": deepinfra, "_groq_key": groq}


# --------------------------------------------------------------- 4. write config

def write_config(answers: dict) -> dict:
    step("Writing ~/.jarvis/config.json and ~/.jarvis/.env")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {k: v for k, v in answers.items() if not k.startswith("_")}
    (STATE_DIR / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    env_lines = []
    if answers.get("_deepinfra_key"):
        env_lines.append(f"DEEPINFRA_API_KEY={answers['_deepinfra_key']}")
    if answers.get("_groq_key"):
        env_lines.append(f"GROQ_API_KEY={answers['_groq_key']}")
    (STATE_DIR / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    ok(f"config written to {STATE_DIR}")
    return cfg


# --------------------------------------------------------------- 5. starter vault

VAULT_CLAUDE_MD = """# {name}'s Second Brain — LLM Wiki

A personal knowledge base that grows as {name} and Jarvis learn things together.
See wiki/ for pages, index.md for the catalog, log.md for the history.
Structure: wiki/{{entities,concepts,topics,sources,connections,queries}}.
"""


def create_vault(vault_dir: str, name: str) -> None:
    step(f"Setting up the ObsiVault at {vault_dir}")
    v = Path(vault_dir)
    if v.exists() and any(v.iterdir()):
        ok("vault already exists and is non-empty — leaving it alone")
        return
    for sub in ("raw/assets", "wiki/entities", "wiki/concepts", "wiki/topics",
               "wiki/sources", "wiki/connections", "wiki/queries"):
        (v / sub).mkdir(parents=True, exist_ok=True)
    (v / "CLAUDE.md").write_text(VAULT_CLAUDE_MD.format(name=name), encoding="utf-8")
    (v / "index.md").write_text(f"# {name}'s Wiki Index\n\nLast updated: —\n\n"
                                f"## Entities\n\n## Concepts\n\n## Topics\n\n"
                                f"## Sources\n\n## Queries\n", encoding="utf-8")
    (v / "log.md").write_text("# Wiki Log\n\nAppend-only chronological record.\n",
                              encoding="utf-8")
    (v / "hot.md").write_text(
        f"# {name} — Hot Context\n\n"
        f"_This is what Jarvis reads first every morning. Keep it current: "
        f"current priorities, standing facts, whatever matters right now._\n",
        encoding="utf-8")
    ok(f"starter vault created ({v})")


# --------------------------------------------------------------- 6-7. index + icons

def build_index() -> None:
    step("Building the knowledge index (first pass — can take a minute)")
    r = subprocess.run([sys.executable, str(SCRIPTS_DIR / "brain.py"), "index"],
                       capture_output=True, text=True, cwd=SCRIPTS_DIR)
    if r.returncode != 0:
        bad("indexing failed:\n" + r.stderr[-600:])
        return
    ok("brain indexed")


def make_icons() -> None:
    step("Generating app icons")
    try:
        from PIL import Image, ImageDraw, ImageFont
        for size in (192, 512):
            img = Image.new("RGB", (size, size), "#05080d")
            d = ImageDraw.Draw(img)
            m = size * 0.08
            d.ellipse([m, m, size - m, size - m], outline="#3ad6c8",
                     width=max(3, size // 40))
            try:
                f = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", int(size * 0.5))
            except OSError:
                f = ImageFont.load_default()
            bb = d.textbbox((0, 0), "J", font=f)
            d.text(((size - bb[2] + bb[0]) / 2 - bb[0], (size - bb[3] + bb[1]) / 2 - bb[1]),
                  "J", font=f, fill="#3ad6c8")
            img.save(STATE_DIR / f"icon-{size}.png")
        ok("icons written")
    except Exception as exc:
        bad(f"icon generation skipped: {exc}")


# --------------------------------------------------------------- 8-9. task + shortcuts

def create_scheduled_task(cfg: dict) -> bool:
    """True on success. Registering a NEW scheduled task needs privileges an
    automated/sandboxed shell may not hold, even though starting or stopping
    an EXISTING task works fine — confirmed by testing THIS exact install
    script's own creation step before shipping it: it failed with 'Access is
    denied' from the very shell that had just been starting and stopping
    King's real Jarvis task all session. Silently claiming success here would
    have been a lie discovered only when Jarvis never started at the next
    login — so this fails LOUD and hands over a command a normal terminal
    (not run by an agent) can complete in ten seconds.
    """
    step('Creating the "Jarvis Server" scheduled task (auto-starts at login)')
    args = f'"{SCRIPTS_DIR / "serve.py"}" --port {cfg.get("port", 8787)} --host 0.0.0.0'
    if cfg.get("mic_device"):
        args += f' --device "{cfg["mic_device"]}"'
    ps = f"""
$action = New-ScheduledTaskAction -Execute "pythonw.exe" -Argument '{args}' -WorkingDirectory "{SCRIPTS_DIR}"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Unregister-ScheduledTask -TaskName "Jarvis Server" -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName "Jarvis Server" -Action $action -Trigger $trigger -Settings $settings -ErrorAction Stop | Out-Null
Start-ScheduledTask -TaskName "Jarvis Server"
"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode == 0:
        ok('scheduled task "Jarvis Server" created and started')
        return True

    bad("automatic creation was denied by this shell's permissions (this is "
        "common when an agent/automation runs the installer)")
    print(f"\n  Open a NORMAL PowerShell window (Start Menu -> type PowerShell -> Enter,\n"
          f"  do NOT run this through an AI agent's shell) and paste this one line:\n\n"
          f'  $a=New-ScheduledTaskAction -Execute "pythonw.exe" -Argument \'{args}\' '
          f'-WorkingDirectory "{SCRIPTS_DIR}"; $t=New-ScheduledTaskTrigger -AtLogOn; '
          f'Register-ScheduledTask -TaskName "Jarvis Server" -Action $a -Trigger $t '
          f'-Settings (New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval '
          f'(New-TimeSpan -Minutes 2)); Start-ScheduledTask -TaskName "Jarvis Server"\n')
    return False


def create_shortcuts(cfg: dict) -> None:
    step("Creating Desktop + Start Menu shortcuts")
    port = cfg.get("port", 8787)
    profile = cfg.get("chrome_profile", "Default")
    ps = f"""
$sh = New-Object -ComObject WScript.Shell
$chrome = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
if (-not (Test-Path $chrome)) {{ $chrome = "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe" }}
foreach ($dir in @([Environment]::GetFolderPath("Desktop"),
                   [Environment]::GetFolderPath("Programs"))) {{
  $lnk = $sh.CreateShortcut((Join-Path $dir "JARVIS.lnk"))
  $lnk.TargetPath = $chrome
  $lnk.Arguments = '--profile-directory={profile} --app=http://127.0.0.1:{port} --window-size=1600,1000'
  $lnk.IconLocation = "{STATE_DIR / 'icon-192.png'}"
  $lnk.Save()
}}
"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode != 0:
        bad("shortcut creation failed:\n" + r.stderr[-500:])
        return
    ok("Desktop + Start Menu shortcuts created")


# --------------------------------------------------------------- 10. summary

def lan_ip() -> str | None:
    """The phone-reachable address — NOT just any non-loopback IPv4.

    Caught in testing: the naive query returns whichever adapter Windows lists
    first, which on a dev machine is a virtual one (WSL/Hyper-V vEthernet) —
    a real address, just not one a phone on the Wi-Fi can ever reach. Filter
    to actual Wi-Fi/Ethernet adapters first.
    """
    ps = (
        "$a = Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object {$_.IPAddress -notmatch '^(127|169)' -and "
        "$_.InterfaceAlias -match '^(Wi-Fi|Ethernet)'} | "
        "Select-Object -First 1 -ExpandProperty IPAddress; "
        "if (-not $a) { $a = Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object {$_.IPAddress -notmatch '^(127|169)' -and "
        "$_.InterfaceAlias -notmatch 'vEthernet|Loopback'} | "
        "Select-Object -First 1 -ExpandProperty IPAddress }; $a"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    ip = r.stdout.strip()
    return ip or None


def main() -> int:
    print("=" * 60)
    print("  JARVIS INSTALLER")
    print("=" * 60)
    verify_layout()
    install_deps()
    answers = interview()
    cfg = write_config(answers)
    create_vault(cfg["vault_dir"], cfg["owner_name"])
    build_index()
    make_icons()
    task_ok = create_scheduled_task(cfg)
    create_shortcuts(cfg)

    ip = lan_ip()
    port = cfg.get("port", 8787)
    print("\n" + "=" * 60)
    print("  DONE" if task_ok else "  DONE — ONE MANUAL STEP LEFT (see above)")
    print("=" * 60)
    if not task_ok:
        print(f"\n  Run the PowerShell command printed above in a normal PowerShell\n"
              f"  window FIRST — Jarvis will not auto-start at login until you do.")
    print(f"\n  Desktop: double-click the JARVIS icon.")
    if ip:
        print(f"  Phone:   open http://{ip}:{port}/phone in Safari/Chrome, then")
        print(f'           "Add to Home Screen".')
    print(f"\n  NOT set up yet — Gmail (read/send email). That needs its own Google\n"
          f"  OAuth client, a separate deliberate step. Everything else — voice,\n"
          f"  memory, research, the HUD, calendar-free operation — works now.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
