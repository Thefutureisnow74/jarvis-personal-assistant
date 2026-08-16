# JARVIS — Personal AI Assistant

A voice-and-display chief-of-staff for your own computer: talks, listens hands-free,
remembers what you tell it, researches the internet, sends email (with a spoken
confirm gate), and runs on your desktop and phone.

## Install on a new computer

```
git clone <this repo's URL> jarvis
cd jarvis
python product/install_jarvis.py
```

The installer asks a few questions (your name, where your knowledge vault should
live, which Chrome profile and microphone are yours, your DeepInfra and Groq API
keys) and sets everything up: dependencies, config, a starter knowledge vault, the
background service, desktop/Start Menu shortcuts, and app icons.

Requires: Windows, Python 3.10+, Google Chrome, a DeepInfra API key (voice) and a
Groq API key (free at console.groq.com/keys — speech-to-text + answering model).

Full device setup (including the phone) is in `product/INSTALL_DEVICES.html`.

Gmail (read + send) needs its own Google OAuth client and is a deliberate,
separate step — not part of the base install.
