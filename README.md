# Local Flow

A free, private Wispr Flow for your Mac. Speech-to-text runs 100% on-device (Whisper small.en on Apple MLX); cleanup runs on-device too.

## Using it
| Do this | Result |
|---|---|
| **Hold Right ⌥ Option**, talk, release | Text is pasted at your cursor |
| **Double-tap Right ⌥**, talk as long as you like, tap once more | Hands-free mode (icon 🟠) |
| **Esc** during hands-free | Cancel without pasting |
| Right ⌥ + another key (e.g. ⌥→) | Works as normal Option — dictation cancels itself |

Menu bar icon: 🎙 ready · 🔴 recording · 🟠 hands-free · ⏳ loading/transcribing · ⚠️ needs attention.

While you talk, a **floating pill** appears at the bottom of the screen with live mic levels (red = holding, orange 🔒 = hands-free, dots = transcribing). Turn it off: 🎙 → Floating indicator.

### Per-app styles (🎙 → Per-app styles, edit with 🎙 → Edit app styles)
| Style | Where | What it does |
|---|---|---|
| casual | Messages, Slack, Telegram, WhatsApp, Discord | One-sentence messages drop the trailing period ("sounds good") |
| formal | Mail, Pages, Word, Outlook, and any window titled Gmail / Google Docs | Capitalized, always ends with punctuation |
| terminal | Terminal, iTerm, Warp, Ghostty | Never pastes a line break (it could run a half-dictated command), no trailing space |
| default | Everything else | Unchanged |
`styles.json` maps app bundle IDs (`apps`) and browser window titles (`window_titles`) to a style.

### History (🎙 → Search history…)
Opens a searchable page of every dictation, newest first, with Copy buttons, the app it went to, and the original transcript before cleanup.

### Things it understands (free, on-device)
- Fillers: "um", "uh", stutters ("the the") are removed
- Corrections: "Tuesday, no wait, Wednesday" → "Wednesday" (also "actually no", "sorry I mean", "or rather")
- "scratch that" / "delete that" → removes the sentence before it
- "new line", "new paragraph", "bullet point"
- Lists: "three things: first…, second…, third…" → numbered list
- Snippets and dictionary (below)

## App & startup (runs itself — no Terminal, no Claude Code needed)
- App: `~/Applications/Local Flow.app`. Starts automatically at login (`~/Library/LaunchAgents/com.jordan.localflow.plist`, listed under System Settings → General → Login Items).
- **Self-healing:**
  - crashes → restarted automatically within seconds (gives up with a notification after 5 rapid crashes)
  - macOS disables the hotkey → re-enabled automatically (checked every 3s)
  - silent mic (iPhone Continuity, Teams/Zoom audio) → switches to the MacBook mic automatically
  - wake from sleep → mics, hotkey and model refreshed
  - starts offline (no Wi-Fi needed at login); model kept warm so it stays fast after long idle
  - missing permission → menu shows ⚠️ and which one; "Fix permissions…" opens the right page
- If anything ever feels off: 🎙 → **Restart Local Flow**. Still broken? 🎙 → **Open log**.
- Quit: 🎙 → Quit Local Flow (stays quit until next login or you open it via Spotlight: ⌘ Space → "Local Flow").
- Project files: `~/local-flow` (shortcut on Desktop). Apps can't read the Desktop folder, which is why it lives here.

### Permissions (System Settings → Privacy & Security)
Turn on **Local Flow** under **Microphone**, **Input Monitoring**, and **Accessibility**. If it's not listed, click **+**, press ⌘⇧G, type `~/Applications`, and pick Local Flow. Quit and reopen Local Flow after changing permissions.

## Dictionary & snippets (menu → Edit dictionary / Edit snippets)
- `dictionary.json` → `words`: names/jargon to spell correctly. `replacements`: fix a specific mishearing every time.
- `snippets.json` → say the trigger phrase, get the expansion — alone ("my email address") or mid-sentence.
Changes apply on the next dictation; no restart needed.

## Optional: Claude cleanup (paid, ~$0.001/dictation)
Better at messy rambling than the local rules. Add `ANTHROPIC_API_KEY=...` to `.env`, buy API credits at console.anthropic.com, then menu → "Use Claude Haiku instead". Falls back to local cleanup automatically if it fails.

## Config (`config.json`)
- `hotkey` — `alt_r` (Right Option), `cmd_r`, `ctrl_r`, `f13`…
- `model` — `models/whisper-small.en-q8` (default: 8-bit copy, same accuracy, 38% faster, half the memory; rebuild with `.venv/bin/python quantize_model.py 8`) or `mlx-community/whisper-small.en-mlx` (original)
- `wired_mb` — memory locked for the model so macOS cannot swap it out (0 = off)
- `sounds`, `local_cleanup`, `claude_cleanup`, `overlay`, `app_styles` — same as the menu toggles
- `restore_clipboard` — put your previous clipboard (including images) back after pasting
- `max_minutes` — hands-free safety cap

Changes to config.json need a restart (Quit, reopen).

## Troubleshooting
- Nothing happens on the hotkey → Input Monitoring / Accessibility not granted to Local Flow.
- Wrong mic → 🎙 → Microphone → pick one (or "Refresh list" after plugging something in).
- Log: `local-flow.log`. History of every dictation (raw + cleaned + timings): `history.jsonl`.
- Debug run in Terminal (quit the app first): `~/local-flow/run.sh`

## Uninstall
    launchctl unload ~/Library/LaunchAgents/com.jordan.localflow.plist; rm ~/Library/LaunchAgents/com.jordan.localflow.plist
    rm -rf ~/Applications/Local\ Flow.app ~/local-flow ~/Desktop/local-flow

## Rebuild the environment
    cd ~/local-flow && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python mlx-whisper sounddevice numpy pynput rumps anthropic
