"""Offline tests for the free cleanup rules and per-app styles.
Run: .venv/bin/python tests/test_rules.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from rules import clean          # noqa: E402
from styles import apply_style   # noqa: E402

CASES = [
    ("Um, so let's meet Tuesday, no wait, Wednesday at 3.", "So let's meet Wednesday at 3."),
    ("Send it to Mike, no wait, send it to Sarah.", "Send it to Sarah."),
    ("Book the flight for Friday. Actually no, Saturday.", "Book the flight for Saturday."),
    ("I think we should raise prices. Scratch that. Let's keep prices the same.",
     "Let's keep prices the same."),
    ("We have to talk to the the client.", "We have to talk to the client."),
    ("I need three things. First, the proposal, second, the invoice, and third, the logo.",
     "I need three things.\n1. The proposal\n2. The invoice\n3. The logo."),
    ("Groceries bullet point eggs bullet point milk", "Groceries\n- Eggs\n- Milk"),
    ("I'll first check the numbers and then send them.", "I'll first check the numbers and then send them."),
    ("No, that won't work for me.", "No, that won't work for me."),
    ("Email me at 10:30 at name@example.com.", "Email me at 10:30 at name@example.com."),
]
STYLE_CASES = [
    ("casual", "Sounds good.", "Sounds good"),
    ("casual", "Sounds good. See you at 3.", "Sounds good. See you at 3."),
    ("formal", "thanks for the update", "Thanks for the update."),
    ("terminal", "first line\nsecond line", "first line second line"),
]

failed = 0
for raw, want in CASES:
    got = clean(raw)
    if got != want:
        failed += 1
        print(f"FAIL clean({raw!r})\n  want {want!r}\n  got  {got!r}")
for style, raw, want in STYLE_CASES:
    got, _ = apply_style(raw, style)
    if got != want:
        failed += 1
        print(f"FAIL {style}({raw!r}) want {want!r} got {got!r}")
total = len(CASES) + len(STYLE_CASES)
print(f"{total - failed}/{total} passed")
sys.exit(1 if failed else 0)
