# Build Your Own Local Flow with Claude Code

A step-by-step guide to building a free, private, Wispr Flow–style dictation app for your Mac — by prompting [Claude Code](https://claude.com/claude-code), not by writing code yourself.

This guide comes from building the app in this repo over a few days. Most of that time went into debugging problems that macOS throws at you, so the most valuable part here is **[The Pitfalls](#the-pitfalls-read-this-first)**: give it to Claude up front and you skip the days of debugging.

> **Just want the app?** You don't need this guide. Follow the [README](README.md) install instead — it takes 5 minutes.

---

## What you'll build

| Phase | What you get | Time |
|---|---|---|
| 0 | Setup | 10 min |
| 1 | Hold a key, talk, release → text pasted at your cursor | 30 min |
| 2 | Free on-device cleanup: fillers, corrections, lists, dictionary, snippets | 30 min |
| 3 | A real Mac app that starts at login and repairs itself | 45 min |
| 4 | Speed: sub-second dictation, even on an 8 GB Mac | 20 min |
| 5 | Polish: floating pill, per-app styles, searchable history | 45 min |
| 6 | One-command installer + publish to GitHub | 30 min |

**You need:** an Apple Silicon Mac (M1 or newer), macOS 14+, a Claude Pro or Max plan (for Claude Code), and a GitHub account for Phase 6. Total cost beyond your Claude plan: $0.

---

## How to use this guide

- Each phase has a **prompt** in a box. Copy it into Claude Code exactly, then answer Claude's questions and test what it built before moving on.
- **Test every phase yourself** with your real voice. Claude can test a lot, but it can't hold your Option key or talk into your mic.
- When something breaks, don't describe it vaguely — say *"it's not working, check the log"*. Every phase below asks Claude to keep a log file, and that log is how every bug in this project got found in minutes.
- Phases build on each other. Do them in order, in the same Claude Code session if you can.

---

## Phase 0 — Setup

1. Install Claude Code: download the **Claude desktop app** from [claude.ai/download](https://claude.ai/download) and open the **Code** tab, or install the CLI from [claude.com/claude-code](https://claude.com/claude-code).
2. Create the project folder **in your home folder** — not Desktop, Documents, Downloads or iCloud (see [Pitfall 2](#2-the-project-folder-is-on-the-desktop--the-app-silently-quits)):
   ```bash
   mkdir ~/local-flow
   ```
3. Open Claude Code in `~/local-flow`.
4. **Paste the pitfalls first.** This is the single best thing you can do:

> **Prompt 0**
> ```
> I'm going to build a Wispr Flow–style dictation app for my Mac with you,
> phase by phase. Before we start, read this list of macOS pitfalls that the
> last person who built this hit. Keep them in mind for every phase, and
> tell me if a choice you're about to make conflicts with one of them.
>
> <paste the entire "The Pitfalls" section from this guide here>
> ```

---

## Phase 1 — Core dictation

> **Prompt 1**
> ```
> Build me a local, private version of Wispr Flow for my Apple Silicon Mac.
> Phase 1 only: core dictation.
>
> - Menu bar app in Python (rumps) that shows 🎙 when ready, 🔴 while recording,
>   ⏳ while transcribing.
> - Hold Right Option to record from the mic (16 kHz mono, sounddevice);
>   release to transcribe with Whisper running locally via mlx-whisper.
> - Paste the result at my cursor: put it on the clipboard, press ⌘V by posting
>   Quartz CGEvents (NOT pynput's keyboard Controller), then restore my previous
>   clipboard — all types, including images — unless I copied something new
>   meanwhile.
> - Use a Python 3.12 virtual env created with uv in .venv (my system Python
>   may be too new for the ML packages).
> - Settings in config.json (hotkey, model). Log everything to local-flow.log
>   and keep a history.jsonl of every dictation with timings.
> - Benchmark whisper base.en, small.en and large-v3-turbo on my Mac first
>   (generate test audio with the `say` command) and pick the best
>   speed/accuracy trade-off. Show me the numbers.
> - Write a run.sh to start it from Terminal for now.
>
> When it's ready, tell me exactly which permissions to grant and to which app.
> ```

**Test it:** run `./run.sh` in **Terminal**, grant Terminal **Microphone**, **Accessibility** and **Input Monitoring** in System Settings → Privacy & Security, quit Terminal fully (⌘Q), reopen, run again. Hold Right ⌥ in a text box and talk.

**Expect:** `small.en` usually wins — in our test the base model wrote "loop and markets" for "loop in Marcus"; small got it right; large-turbo was 2× slower and *worse* on custom names.

---

## Phase 2 — Free cleanup, dictionary & snippets

> **Prompt 2**
> ```
> Phase 2: make the output clean without paying for an AI API.
>
> Write rules.py with conservative, offline cleanup rules (when unsure, leave
> the text alone):
> - remove fillers (um, uh, erm) and stutters ("the the"), but not "had had"
> - self-corrections: "Tuesday, no wait, Wednesday" -> "Wednesday";
>   "send it to Mike, no wait, send it to Sarah" -> "send it to Sarah"
>   (cues: no wait, wait no, actually no, sorry I mean, or rather…)
> - "scratch that" / "delete that" removes the sentence before it
> - "new line", "new paragraph", "bullet point"
> - "three things: first …, second …, third …" -> numbered list
>   (only with 3+ ordinals, so "I'll first check" stays a sentence)
> - tidy spacing, capitalization and punctuation
>
> Also:
> - dictionary.json: "words" fed to Whisper's initial_prompt so names are spelled
>   right, plus "replacements" for words it keeps mishearing
> - snippets.json: spoken trigger -> text ("my email address" -> my email),
>   working alone or mid-sentence (swap in placeholders before cleanup)
> - optional Claude Haiku cleanup if ANTHROPIC_API_KEY is set in .env, falling
>   back to the local rules on any error — off by default
> - menu items to toggle cleanup and open the dictionary/snippets files
>
> Write tests/test_rules.py with at least 10 realistic dictations and run it.
> ```

**Test it:** dictate *"Um so let's meet Tuesday, no wait, Wednesday at 3. I need three things: first the proposal, second the invoice, third the logo."* — you should get one corrected sentence and a numbered list.

---

## Phase 3 — A real app that runs itself

This is the phase where macOS fights back. Every bullet in this prompt is a bug we hit for real.

> **Prompt 3**
> ```
> Phase 3: turn this into a real Mac app that starts at login and repairs
> itself, so I never have to open Terminal or Claude Code to fix it.
>
> App bundle:
> - Build "Local Flow.app" in ~/Applications with an Info.plist (LSUIElement,
>   NSMicrophoneUsageDescription), ad-hoc codesigned.
> - Its executable is a zsh launcher that runs the venv's python as a CHILD
>   process (not exec) so permissions are filed under "Local Flow", and
>   restarts it if it crashes (exit 0 = user quit; give up with a notification
>   after 5 quick crashes).
> - Start at login with a LaunchAgent that runs `open -a` on the app.
> - Single-instance lock so two copies can never paste twice.
> - Hide the Dock icon (accessory activation policy).
>
> Reliability:
> - Replace any pynput listener with our own Quartz CGEventTap in hotkey.py:
>   listen-only, keep the callback tiny and hand events to a worker thread via
>   a queue, re-enable the tap on kCGEventTapDisabledByTimeout/UserInput, and a
>   watchdog every 3s that re-enables or rebuilds it. Tell left/right modifiers
>   apart with device-dependent flag bits. Ignore events from our own pid.
> - Permissions: on start, check Accessibility (AXIsProcessTrustedWithOptions
>   with prompt), Input Monitoring (IOHIDCheckAccess/IOHIDRequestAccess) and
>   Microphone (AVCaptureDevice authorizationStatus / requestAccess). Show ⚠️
>   and which permission is missing in the menu, a "Fix permissions…" item that
>   opens the right System Settings pane, and restart automatically once granted.
> - Mic: if the chosen mic delivers no audio or all-zero samples for 0.5s,
>   switch to the built-in MacBook mic and remember that device as silent.
>   Add a Microphone submenu. Refresh the device list after each dictation.
> - Ignore near-silent recordings and Whisper's "Thank you." hallucinations.
> - If Right Option is pressed together with another key, cancel (it's being
>   used as a modifier). Double-tap = hands-free mode, tap again to finish,
>   Esc to cancel.
> - Opt out of App Nap. Refresh mics/hotkey on wake from sleep.
> - Start fully offline (HF_HUB_OFFLINE=1, download only if the model is missing).
> - Never touch AppKit UI from background threads — queue UI changes and apply
>   them from a main-thread timer.
> - Menu: Restart Local Flow, Open log.
>
> The project lives in ~/local-flow, not the Desktop. Test everything you can
> yourself (the hotkey tap with synthesized events, mic fallback using a
> silent virtual device) from my Terminal pane, then tell me what I need to test.
> ```

**Test it:** quit the Terminal version first. Open Local Flow from Spotlight, grant the three permissions to **Local Flow** this time (not Terminal), dictate a few times, put the Mac to sleep and wake it, try again. Then restart your Mac and check it comes back on its own.

---

## Phase 4 — Speed

If your dictations sometimes take 5–8 seconds, it's not Whisper being slow — it's your Mac swapping the model out to disk.

> **Prompt 4**
> ```
> Phase 4: dictation is sometimes slow. Diagnose before changing anything:
> show me the stt timings from history.jsonl, the app's physical footprint
> (vmmap --summary) and the system swap usage.
>
> Then:
> - Make an 8-bit quantized copy of whisper small.en with mlx (quantize_model.py),
>   and benchmark fp16 vs 8-bit vs 4-bit on accuracy (word error rate on test
>   clips) and speed. Use the best one.
> - mx.set_wired_limit so the model can't be paged out.
> - mx.set_cache_limit (~64 MB) — MLX keeps hundreds of MB of scratch buffers
>   after each run otherwise.
> - When the hotkey goes down, prefetch: run a tiny transcription in the
>   background so the model is paged in while I'm still talking.
> - Keep-warm every 90 seconds while idle.
> Show me before/after memory and timings.
> ```

**What we measured:** 8-bit had the same accuracy as full precision and was 38% faster; MLX's scratch buffers dropped from 799 MB to 68 MB; the app went from 1.4 GB to 0.5 GB of memory.

---

## Phase 5 — Polish

> **Prompt 5**
> ```
> Phase 5: polish, Wispr-style.
>
> 1. overlay.py — a floating pill near the bottom-center of the screen while
>    recording: a borderless non-activating NSPanel (never takes focus or
>    clicks, visible on all Spaces and over full-screen apps) with live mic
>    level bars. Red while holding, orange with a 🔒 in hands-free, pulsing dots
>    while transcribing. Render each state to a PNG offscreen and show me
>    before wiring it in.
> 2. styles.py — per-app styles chosen from the app in front at paste time
>    (and the focused window title via Accessibility, so Gmail in Chrome counts
>    as email): casual (chat apps: drop the period on one-sentence messages),
>    formal (mail/docs: capitalized, ends with punctuation), terminal (never
>    paste a newline — it could run a half-dictated command), default.
>    Mapping in styles.json, editable from the menu.
> 3. history_view.py — a self-contained offline HTML page of all dictations:
>    search with highlighting, grouped by day, Copy buttons, the app it went
>    to, and the original transcript before cleanup. Works in light and dark mode.
> Add menu toggles for the pill and per-app styles.
> ```

---

## Phase 6 — Share it on GitHub

> **Prompt 6**
> ```
> Phase 6: package this so friends can install it with one command, then
> publish it to my GitHub.
>
> - install.sh: check Apple Silicon and that the folder isn't in a protected
>   location (Desktop/Documents/Downloads/iCloud), install uv if missing (ask
>   first), create the venv from a pinned requirements.txt, build the 8-bit
>   model, build and sign the .app, install the login item, launch it, and
>   print the permission steps. Safe to re-run. Add a test mode that builds to
>   a scratch location without touching my running app.
> - uninstall.sh (keeps settings unless --all).
> - Personal files (config.json, dictionary.json, snippets.json, styles.json,
>   .env, history, logs, models, .venv) must be gitignored; ship
>   *.example.json versions and create the real ones on first run.
> - Scan every tracked file for my personal info (name, email, API keys)
>   before committing. Use my GitHub no-reply email for commits.
> - README for people who just want to install it; MIT license.
> - Test the installer on a fresh clone in test mode.
> - Show me exactly what will be published and ASK before creating the public
>   repo and pushing.
> ```

---

## The Pitfalls (read this first)

Every one of these happened while building this app. Symptom → cause → fix.

#### 1. Python packages won't install
**Cause:** your system Python is newer than the ML packages support. **Fix:** use `uv venv --python 3.12`.

#### 2. The project folder is on the Desktop → the app silently quits
**Symptom:** works from Terminal, but the `.app` launches and exits with no error. **Cause:** macOS privacy protection blocks apps from reading Desktop, Documents, Downloads and iCloud Drive. **Fix:** keep the project in `~/local-flow`.

#### 3. Permissions belong to whatever launched the code
**Symptom:** worked from Terminal, dead as an app (or vice versa). **Cause:** Terminal and "Local Flow" are different apps to macOS. Also, an app that was *already running* when you granted a permission doesn't get it. **Fix:** grant to the app that actually runs it, then restart it. Have the app prompt for permissions itself (`AXIsProcessTrustedWithOptions`, `IOHIDRequestAccess`, `AVCaptureDevice.requestAccess`) so macOS adds it to the lists for you, and restart itself once granted. If a permission is "on" but not working, remove the entry with **–** and re-add it (stale entry from an older build).

#### 4. The hotkey stops working after a while, silently
**Cause:** macOS disables a keyboard event tap whose callback is slow (memory pressure, after sleep) — pynput never re-enables it, and any exception in a pynput callback kills its listener for good. **Fix:** your own `CGEventTap` that re-enables itself on `kCGEventTapDisabledByTimeout`, a watchdog that rebuilds it, and zero real work inside the callback (queue events to a worker thread).

#### 5. The app crashes every time it pastes (exit code 133 / SIGTRAP)
**Cause:** on macOS 26, pynput's keyboard `Controller` looks up the keyboard layout (Text Input Sources) from a background thread, which macOS now kills instantly. **Fix:** post ⌘V yourself with Quartz `CGEventCreateKeyboardEvent` + `CGEventPost` (keycode 9 + command flag). Find crashes like this in `~/Library/Logs/DiagnosticReports/python3.12-*.ips` — ask Claude to read the faulting thread.

#### 6. "Recorded 0.0s" or no speech, even though the mic is allowed
**Cause:** your default input can be a "mic" that never sends audio — the iPhone Continuity microphone, Microsoft Teams Audio, Zoom's virtual device. Some send nothing, some send perfect digital silence (all zeros; real mics always have some noise). **Fix:** detect no audio *or* all-zero audio in the first 0.5s and fall back to the built-in mic; remember silent devices.

#### 7. Random "Thank you." pasted from silence
**Cause:** Whisper hallucinates on near-silent audio. **Fix:** measure how much actual speech is in the clip first and skip if under ~0.25s; drop known hallucinations when speech is short.

#### 8. Option-key shortcuts start a recording
**Cause:** Right Option is also a modifier (⌥→ to jump words). **Fix:** if any other key is pressed while it's held, cancel the recording.

#### 9. Dictation wipes your clipboard
**Fix:** snapshot every pasteboard item and type with `NSPasteboard`, restore after ~0.5s, and only if `changeCount` shows you didn't copy something new in the meantime.

#### 10. Sometimes it takes 6–8 seconds
**Cause:** not Whisper — an 8 GB Mac running out of memory swaps the model to disk (check `sysctl vm.swapusage`; Chrome is usually the biggest user). Also App Nap throttles background apps, and MLX keeps ~800 MB of scratch buffers after every run. **Fix:** 8-bit model, `mx.set_wired_limit`, `mx.set_cache_limit`, prefetch the model when the key goes down, keep-warm, opt out of App Nap.

#### 11. Doesn't start at login when Wi-Fi isn't up yet
**Cause:** the Hugging Face library checks online for model updates on load. **Fix:** `HF_HUB_OFFLINE=1`, falling back to online only if the model isn't downloaded.

#### 12. Random crashes around the menu bar
**Cause:** AppKit UI touched from a background thread. **Fix:** worker threads only set state; a main-thread timer applies it to the menu/icon/pill.

#### 13. Everything pastes twice
**Cause:** two copies running (one from Terminal, one as the app). **Fix:** a single-instance file lock.

#### 14. The app can't be launched from Claude Code
**Cause:** Claude Code's sandboxed shell can't `open` apps or use your mic/keyboard. **Fix:** ask Claude to run live tests and launches in your **Terminal pane**, where your permissions apply.

#### 15. A dictated "new line" in Terminal runs a command
**Fix:** a terminal style that turns line breaks into spaces before pasting.

---

## Tips for working with Claude Code on this

- **Ask for evidence.** "Show me the numbers", "render it to a PNG and show me", "run it on a fresh clone". Claude is much better when it checks its own work.
- **Ask it to diagnose before fixing.** For anything flaky: "check the log and crash reports first, tell me the cause, then fix it."
- **Keep one session per phase or two** so Claude remembers what it built; start a new one if it gets slow, and say "read README.md and GUIDE.md first".
- **Commit after each working phase** (`git add -A && git commit -m "phase N"`), so you can always go back.
- **Don't fight the model choice.** `small.en` in 8-bit is the sweet spot on M-series Macs. Bigger isn't better here.

Happy dictating. 🎙
