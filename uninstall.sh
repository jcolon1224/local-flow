#!/bin/zsh
# Removes the Local Flow app and login item. Leaves this folder (your settings,
# history and the downloaded model) unless you pass --all.
cd "$(dirname "$0")"
DIR="$(pwd -P)"

pkill -f "Local Flow.app/Contents/MacOS/LocalFlow" 2>/dev/null
pkill -f "$DIR/flow.py" 2>/dev/null
for agent in ~/Library/LaunchAgents/*localflow*.plist(N); do
  launchctl bootout "gui/$(id -u)" "$agent" 2>/dev/null
  rm -f "$agent" && echo "Removed login item $(basename "$agent")"
done
rm -rf "$HOME/Applications/Local Flow.app" && echo "Removed ~/Applications/Local Flow.app"

if [[ "$1" == "--all" ]]; then
  rm -rf "$DIR/.venv" "$DIR/models" "$DIR"/history.* "$DIR"/local-flow.log*
  echo "Removed the Python environment, model, history and logs."
  echo "The downloaded original model may also be in ~/.cache/huggingface (safe to delete)."
fi
echo "Done. Permissions entries in System Settings can be removed with the – button."
