#!/bin/zsh
# Local Flow installer — free, private, on-device dictation for Apple Silicon Macs.
#
#   ./install.sh
#
# Safe to re-run (it updates in place). What it does:
#   1. checks your Mac (Apple Silicon, a folder apps are allowed to read)
#   2. installs Python 3.12 + dependencies into .venv (via uv)
#   3. downloads Whisper small.en (~480 MB) and makes an 8-bit copy (~260 MB)
#   4. builds "Local Flow.app" in ~/Applications
#   5. starts it now and at every login
set -e
cd "$(dirname "$0")"
DIR="$(pwd -P)"
APP="${LOCALFLOW_APP:-$HOME/Applications/Local Flow.app}"
TEST_MODE="${LOCALFLOW_TEST:-0}"  # 1 = build only: don't touch the running app or login items
USER_ID="$(id -un | tr -cd 'a-zA-Z0-9')"
# Keep the bundle id of an existing install: macOS files Microphone/Accessibility/
# Input Monitoring permissions under it, and changing it would revoke them.
BUNDLE_ID="$(defaults read "$APP/Contents/Info" CFBundleIdentifier 2>/dev/null || true)"
[[ -n "$BUNDLE_ID" ]] || BUNDLE_ID="com.localflow.${USER_ID:-user}"
AGENT="$HOME/Library/LaunchAgents/$BUNDLE_ID.plist"

step() { print -P "\n%F{cyan}==>%f %B$1%b"; }
fail() { print -P "%F{red}✗ $1%f"; exit 1; }
ok()   { print -P "%F{green}✓%f $1"; }

# ---------------------------------------------------------------- 1. checks
step "Checking your Mac"
[[ "$(uname)" == "Darwin" ]] || fail "Local Flow only runs on macOS."
[[ "$(uname -m)" == "arm64" ]] || fail "Local Flow needs an Apple Silicon Mac (M1 or newer)."
ok "Apple Silicon Mac, macOS $(sw_vers -productVersion)"

# macOS blocks apps from reading these folders, so the app would silently quit.
for protected in Desktop Documents Downloads "Library/Mobile Documents"; do
  if [[ "$DIR" == "$HOME/$protected"* ]]; then
    fail "This folder is inside ~/$protected, which macOS doesn't let apps read.
  Move it first, e.g.:  mv \"$DIR\" ~/local-flow && cd ~/local-flow && ./install.sh"
  fi
done
ok "Project folder: $DIR"

# ---------------------------------------------------------------- 2. python
step "Installing Python and dependencies"
if ! command -v uv >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/uv" ]]; then
  print "Local Flow uses 'uv' (a fast Python installer from astral.sh) to set up Python."
  read "reply?Install uv now? [y/N] "
  [[ "$reply" == [yY]* ]] || fail "uv is required. Install it from https://docs.astral.sh/uv/ and re-run."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
[[ -d .venv ]] || "$UV" venv --python 3.12 .venv
"$UV" pip install --quiet --python .venv/bin/python -r requirements.txt
ok "Python environment ready (.venv)"

for f in config dictionary snippets; do
  [[ -f "$f.json" ]] || cp "$f.example.json" "$f.json"
done
[[ -f .env ]] || { touch .env; chmod 600 .env; }

# ---------------------------------------------------------------- 3. model
step "Preparing the speech model"
if [[ -f models/whisper-small.en-q8/weights.safetensors ]]; then
  ok "8-bit Whisper model already built"
else
  print "Downloading Whisper small.en (~480 MB, one time)…"
  .venv/bin/python quantize_model.py 8
  ok "8-bit Whisper model built (same accuracy, faster, half the memory)"
fi

# ---------------------------------------------------------------- 4. app
step "Building Local Flow.app"
# Build into a staging folder, then replace the real app only if something changed:
# re-signing an unchanged bundle is pointless and every change risks a permission re-prompt.
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/localflow.XXXXXX")/Local Flow.app"
mkdir -p "$STAGE/Contents/MacOS" "$HOME/Library/LaunchAgents"
cat > "$STAGE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Local Flow</string>
  <key>CFBundleDisplayName</key><string>Local Flow</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>LocalFlow</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>3.0</string>
  <key>CFBundleShortVersionString</key><string>3.0</string>
  <key>LSMinimumSystemVersion</key><string>13.5</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Local Flow records your voice while you hold the dictation key and transcribes it on this Mac.</string>
</dict>
</plist>
PLIST

# The launcher keeps Python as a *child* process (not exec) so macOS files the
# Microphone / Accessibility / Input Monitoring permissions under "Local Flow",
# and restarts it if it crashes — with a backoff, never giving up for good.
# Quitting from the menu (exit 0) really quits.
cat > "$STAGE/Contents/MacOS/LocalFlow" <<LAUNCHER
#!/bin/zsh
DIR="$DIR"
LOG="\$DIR/local-flow.log"
cd "\$DIR" || exit 1
[ -f .env ] && set -a && source .env && set +a
CHILD=""
trap '[ -n "\$CHILD" ] && kill \$CHILD 2>/dev/null; exit 0' TERM INT
[ -f "\$LOG" ] && [ \$(stat -f%z "\$LOG") -gt 2000000 ] && mv "\$LOG" "\$LOG.old"
quick_crashes=0
notified=0
while true; do
  started=\$(date +%s)
  "\$DIR/.venv/bin/python" "\$DIR/flow.py" >> "\$LOG" 2>&1 &
  CHILD=\$!
  wait \$CHILD
  code=\$?
  [ \$code -eq 0 ] && exit 0
  echo "\$(date '+%F %T') launcher: Local Flow exited (code \$code) — restarting" >> "\$LOG"
  # Crashes right after start count up; a run that lasted a while resets the count.
  if (( \$(date +%s) - started < 60 )); then quick_crashes=\$((quick_crashes + 1)); else quick_crashes=0; notified=0; fi
  if (( quick_crashes >= 5 && notified == 0 )); then
    osascript -e 'display notification "Local Flow keeps crashing; retrying every minute. See 🎙 → Open log." with title "Local Flow"' 2>/dev/null
    notified=1
  fi
  delay=\$((2 * quick_crashes * quick_crashes))   # 0, 2, 8, 18, 32, 50…
  (( delay > 60 )) && delay=60
  sleep \$delay
done
LAUNCHER
chmod +x "$STAGE/Contents/MacOS/LocalFlow"
if [[ -d "$APP" ]] && cmp -s "$STAGE/Contents/Info.plist" "$APP/Contents/Info.plist" \
   && cmp -s "$STAGE/Contents/MacOS/LocalFlow" "$APP/Contents/MacOS/LocalFlow"; then
  rm -rf "$(dirname "$STAGE")"
  ok "$APP is already up to date"
else
  if [[ "$TEST_MODE" != 1 ]]; then
    pkill -f "$DIR/flow.py" 2>/dev/null || true
    pkill -f "Local Flow.app/Contents/MacOS/LocalFlow" 2>/dev/null || true
  fi
  codesign --force --deep -s - "$STAGE" >/dev/null 2>&1
  rm -rf "$APP"
  mv "$STAGE" "$APP"
  rm -rf "$(dirname "$STAGE")"
  ok "Built $APP"
fi

if [[ "$TEST_MODE" == 1 ]]; then
  ok "Test mode: skipping login item and launch"
  exit 0
fi

# ---------------------------------------------------------------- 5. login
step "Starting at login"
# One login item only: remove agents left by older installs under other names.
for old in "$HOME"/Library/LaunchAgents/*localflow*.plist(N); do
  [[ "$old" == "$AGENT" ]] && continue
  launchctl bootout "gui/$(id -u)" "$old" 2>/dev/null || true
  rm -f "$old"
done
cat > "$AGENT" <<AGENTPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$BUNDLE_ID</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/open</string><string>-a</string><string>$APP</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
AGENTPLIST
ok "Login item installed (System Settings → General → Login Items)"

if pgrep -f "$DIR/flow.py" >/dev/null; then
  ok "Local Flow is already running — restart it from 🎙 → Restart Local Flow to pick up changes"
else
  open "$APP"
  ok "Local Flow is starting — look for 🎙 in the menu bar (top-right)"
fi

cat <<'DONE'

────────────────────────────────────────────────────────────────────
 One-time permissions (macOS will prompt; or 🎙 → Fix permissions…)
   System Settings → Privacy & Security → turn on "Local Flow" under:
     • Microphone      • Accessibility      • Input Monitoring
   The app restarts itself as soon as they're granted.

 Then: click into any text box, HOLD Right ⌥ Option, talk, let go.
────────────────────────────────────────────────────────────────────
DONE
