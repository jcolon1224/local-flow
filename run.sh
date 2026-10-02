#!/bin/zsh
# Start Local Flow (menu bar app). Keep this Terminal window open while using it.
cd "$(dirname "$0")"
# Optional: ANTHROPIC_API_KEY=... in .env enables the Claude cleanup pass.
[ -f .env ] && set -a && source .env && set +a
exec .venv/bin/python flow.py
