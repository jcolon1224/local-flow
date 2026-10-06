# Local Flow 🎙

**Free, private voice dictation for your Mac.** Hold a key, talk, let go — your words appear wherever your cursor is, cleaned up and punctuated. Like [Wispr Flow](https://wisprflow.ai), but it runs **entirely on your Mac**: no subscription, no account, and your voice never leaves your computer.

- ⚡ **Fast** — recording starts in ~50 ms, transcription takes about half a second (Whisper on Apple's MLX)
- 🔒 **Private** — speech-to-text and cleanup run on-device; works offline
- 🧹 **Understands how people talk** — removes "um/uh", applies "Tuesday, no wait, Wednesday", "scratch that", "new paragraph", spoken lists
- 💬 **Adapts to the app** — casual in Messages/Slack, proper punctuation in Mail/Gmail, safe in Terminal
- 🛠 **Takes care of itself** — starts at login, restarts if it crashes, recovers the hotkey and mic automatically, survives damaged settings files, and never freezes on a hung audio system

> Want to learn how it's built, or build your own version with Claude Code? See **[GUIDE.md](GUIDE.md)** — step-by-step prompts plus every pitfall we hit.

---

## Requirements
- A Mac with **Apple Silicon** (M1, M2, M3, M4…). Intel Macs aren't supported.
- macOS 14 Sonoma or newer (tested on macOS 26)
- ~2 GB free disk space, 8 GB RAM or more
- An internet connection for the one-time install

## Install (about 5 minutes)

Open **Terminal** (⌘ Space → type "Terminal" → Return) and paste these lines one at a time:

```bash
git clone https://github.com/jcolon1224/local-flow.git ~/local-flow
```
```bash
cd ~/local-flow && ./install.sh
```

> **Important:** keep the folder at `~/local-flow` (or anywhere that's *not* Desktop, Documents, Downloads or iCloud Drive). macOS blocks apps from reading those folders and Local Flow would silently quit. The installer checks this for you.

If you don't have `git`, macOS will offer to install the Command Line Tools — click **Install**, wait, then run the line again.

### Grant three permissions (one time)
macOS will ask, or click 🎙 in the menu bar → **Fix permissions…**. In **System Settings → Privacy & Security**, turn on **Local Flow** under:

1. **Microphone** — to hear you
2. **Accessibility** — to paste text for you
3. **Input Monitoring** — to notice the hotkey

Local Flow restarts itself as soon as they're on. The 🎙 icon in the top-right menu bar means it's ready.

## Use it

| Do this | Result |
|---|---|
| **Hold Right ⌥ Option**, talk, let go | Text appears at your cursor |
| **Double-tap Right ⌥**, talk as long as you like, tap again | Hands-free mode |
| **Esc** during hands-free | Cancel |
| Right ⌥ + another key (e.g. ⌥→) | Works as a normal Option key |

A small pill at the bottom of the screen shows your voice level while recording (red = holding, orange 🔒 = hands-free, dots = transcribing).

**Things you can say:**
- "Let's meet Tuesday, **no wait**, Wednesday" → *Let's meet Wednesday*
- "…**scratch that**" → deletes the previous sentence
- "**new line**", "**new paragraph**", "**bullet point**"
- "Three things: **first** … **second** … **third** …" → a numbered list
- Your own **snippets**: "my email address" → *you@example.com*

## Make it yours (🎙 menu)

| Menu item | What it's for |
|---|---|
| **Search history…** | Every dictation, searchable, with Copy buttons |
| **Edit dictionary** | Names and jargon to spell right (`words`), and fixes for words it keeps mishearing (`replacements`) |
| **Edit snippets** | Phrases that expand to text: email, address, calendar link, sign-off |
| **Edit app styles** | Which apps get casual / formal / terminal style |
| **Microphone** | Pick a specific mic (default: automatic, skipping silent ones) |
| Smart cleanup / Per-app styles / Floating indicator / Sounds | On/off switches |
| **Restart Local Flow** / **Open log** | If anything ever feels off |

Other settings (hotkey, model, memory) live in `~/local-flow/config.json` (defaults in [config.example.json](config.example.json)); restart after changing. Supported hotkeys: `alt_r` (default), `cmd_r`, `ctrl_r`, `shift_r`, `fn`, `f13`–`f19`.

### Optional: Claude cleanup (paid)
The free cleanup handles most things. For messier, rambling speech you can have Claude Haiku tidy the *text* (never the audio) for about $0.001 per dictation: create an API key at [console.anthropic.com](https://console.anthropic.com), add credits, put `ANTHROPIC_API_KEY=sk-ant-…` in `~/local-flow/.env`, restart, and turn on 🎙 → **Use Claude Haiku instead**.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No 🎙 in the menu bar | ⌘ Space → "Local Flow". On notched MacBooks, the icon can hide behind the notch — quit a few other menu bar apps. |
| ⚠️ instead of 🎙 | Click it — the top line says which permission is missing. Use **Fix permissions…** |
| Nothing happens when holding the key | Accessibility / Input Monitoring not on for Local Flow. If it's on but still dead, select it, click **–**, then **Fix permissions…** to re-add. |
| "Didn't catch any speech" | 🎙 → Microphone → pick your mic (iPhone and Teams/Zoom "mics" are often silent) |
| Slow sometimes | Your Mac is low on memory. Close Chrome tabs or turn on Chrome → Settings → Performance → Memory Saver. |
| Anything else | 🎙 → **Restart Local Flow**, then 🎙 → **Open log** |

## Update
```bash
cd ~/local-flow && git pull && ./install.sh
```
Your settings, dictionary, snippets and history are kept.

## Uninstall
```bash
~/local-flow/uninstall.sh --all && rm -rf ~/local-flow
```

## How it works

| File | Role |
|---|---|
| `flow.py` | Menu bar app: recording, the hotkey state machine, transcription queue, paste |
| `audio.py` | Microphone capture on Apple's AVAudioEngine, mic list via CoreAudio, silent-mic fallback |
| `hotkey.py` | Global hotkey via a macOS event tap that re-enables itself if macOS disables it |
| `rules.py` | Free on-device cleanup (fillers, corrections, lists, spoken formatting) |
| `cleanup.py` | Snippets, dictionary, optional Claude Haiku pass |
| `styles.py` | Per-app styles (detects the app and browser tab you're typing into) |
| `overlay.py` | The floating recording pill |
| `history_view.py` | Builds the searchable history page |
| `quantize_model.py` | Makes the 8-bit copy of Whisper small.en |
| `install.sh` | Builds the Python env, model, `Local Flow.app` and login item |

Speech recognition: OpenAI's [Whisper](https://github.com/openai/whisper) `small.en` via Apple's [MLX](https://github.com/ml-explore/mlx) ([mlx-whisper](https://pypi.org/project/mlx-whisper/)).

## License
MIT — free to use, change and share. Personal project, not affiliated with Wispr.
