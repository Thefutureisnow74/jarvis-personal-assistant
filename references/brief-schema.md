# `brief.json` — the one artifact both the HUD and the voice read

Write it to `~/.jarvis/brief.json`. Every field is optional except `spoken`; the HUD
degrades gracefully and shows an italic "empty" line for anything missing.

```json
{
  "greeting": "Good morning, sir",
  "date_human": "Thursday, August 14",
  "clock": "07:42 CT",

  "main_goal": "Finish the Hermes agent course",
  "steps": [
    { "text": "Record the Jarvis build tutorial", "done": false },
    { "text": "Show Hermes and Fable together",   "done": false },
    "Send the cut to the editor"
  ],

  "agenda": [
    { "time": "09:30", "title": "Deep work — Hermes course",
      "added_by_jarvis": true, "note": "blocked for step 1" },
    { "time": "19:00", "title": "Friend's birthday dinner",
      "hard_commitment": true, "location": "Dallas", "note": "45 min commute" }
  ],

  "notes": ["Wrapping at 17:00 instead of 18:30 to clear the commute."],

  "calendar_actions": [
    "Created 09:30–11:00 '🎯 Record the Jarvis build tutorial'",
    "Moved '⏳ Admin sweep' from 17:00 to 16:00"
  ],

  "context_points": [
    "Merchandise Return case MER-3 PPG Paint Sprayer — vendor reply still outstanding"
  ],

  "systems": ["Google Calendar: connected", "Computer use: opened VS Code"],

  "voice": { "provider": "sapi" },

  "spoken": "Good morning, sir. I've gone ahead and organized your calendar. ..."
}
```

## Field notes

- **`steps`** — a string or `{text, done}`. Done steps render struck through.
- **`agenda[].added_by_jarvis`** — marks the block Jarvis wrote, badged `SCHEDULED`.
  Set it only on events you actually created this run.
- **`agenda[].hard_commitment`** — a person is attached; badged `HARD STOP`. This is
  what justifies pulling the wrap time earlier.
- **`calendar_actions`** — the receipts for pillar 2. An empty list renders "No calendar
  writes this run", which is exactly the honest signal wanted when the write failed.
- **`voice.provider`** — set from `speak.py`'s JSON output *after* it runs, so the footer
  reports what really spoke. Re-render the HUD after speaking if you want it exact.
- **`spoken`** — the script. Butler register, "sir", leads with what was already done,
  45–75 seconds, closes on the single next action. Nothing in here may be invented.
