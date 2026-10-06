"""Damaged settings files must never take the app down. Runs offline in a temp
copy of the project so your real settings are untouched."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
PY = ROOT / ".venv/bin/python"
results = []


def check(name, cond):
    results.append(cond)
    print(("PASS " if cond else "FAIL ") + name)


def run(snippet: str, files: dict[str, str]) -> str:
    """Run `snippet` inside a scratch copy of the project with the given files."""
    tmp = Path(tempfile.mkdtemp())
    for f in ("flow.py", "audio.py", "cleanup.py", "rules.py", "styles.py", "hotkey.py",
              "overlay.py", "history_view.py", "config.example.json",
              "dictionary.example.json", "snippets.example.json"):
        shutil.copy(ROOT / f, tmp / f)
    for name, content in files.items():
        (tmp / name).write_text(content)
    proc = subprocess.run([str(PY), "-c", snippet], cwd=tmp, capture_output=True, text=True,
                          env={"HF_HUB_OFFLINE": "1", "PATH": "/usr/bin:/bin"})
    shutil.rmtree(tmp, ignore_errors=True)
    return (proc.stdout + proc.stderr).strip().splitlines()[-1] if (proc.stdout + proc.stderr).strip() else "<no output>"


IMPORT = "import flow; print(flow.CONFIG['max_minutes'], flow.CONFIG['hotkey'], flow.CONFIG_ERROR)"
out = run(IMPORT, {"config.json": "{ this is not json"})
check(f"garbage config.json -> defaults + message ({out})", out.startswith("10 alt_r config.json ignored"))
out = run(IMPORT, {"config.json": "[1, 2, 3]"})
check(f"config.json that is a list -> defaults ({out})", out.startswith("10 alt_r config.json ignored"))
out = run(IMPORT, {"config.json": json.dumps({"max_minutes": "ten", "hotkey": "alt_r"})})
check(f"wrong-typed value -> that key defaults ({out})", out.startswith("10 alt_r config.json: max_minutes"))
out = run(IMPORT, {"config.json": json.dumps({"max_minutes": "7", "sounds": 0})})
check(f"string number is coerced ({out})", out.startswith("7 alt_r None"))

CLEAN = ("from cleanup import Cleaner, dictionary_words; c = Cleaner(True, False); "
         "print(repr(c.clean('um send it to my email address please')), dictionary_words())")
out = run(CLEAN, {"dictionary.json": "[\"a\", \"list\"]", "snippets.json": "null"})
check(f"dictionary/snippets of the wrong shape are ignored ({out})", "Send it to my email address please" in out and "[]" in out)
out = run(CLEAN, {"dictionary.json": json.dumps({"words": ["Ok", 5, ""], "replacements": {"x": 1, "": "y"}}),
                  "snippets.json": json.dumps({"": "boom", "my email address": "a@b.c", "k": 3})})
check(f"bad entries skipped, good ones kept ({out})", "Send it to a@b.c please" in out and "['Ok']" in out)

STYLE = "from styles import load_styles, apply_style; s = load_styles(); print(type(s['apps']).__name__, apply_style('hi there.', s['apps'].get('nope', 'default'))[0])"
out = run(STYLE, {"styles.json": "[]"})
check(f"styles.json that is a list -> defaults ({out})", out == "dict hi there.")
out = run(STYLE, {"styles.json": json.dumps({"apps": "broken", "window_titles": {"Gmail": "formal"}})})
check(f"damaged styles section -> default section ({out})", out == "dict hi there.")

print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
