"""Live test of hotkey.py: run from a terminal that has Accessibility access.
Synthesizes Right-Option presses from a child process, and checks recovery
after the tap is disabled (what macOS does to slow taps)."""
import logging, subprocess, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
import Quartz as Q
from hotkey import HotkeyTap

got = []
tap = HotkeyTap("alt_r", lambda k, key, ts: got.append((k, key)))

POSTER = r'''
import Quartz as Q, time, sys
def mod(down):
    e = Q.CGEventCreateKeyboardEvent(None, 61, down)
    Q.CGEventSetType(e, Q.kCGEventFlagsChanged)
    Q.CGEventSetFlags(e, 0x80040 if down else 0x100)
    Q.CGEventPost(Q.kCGSessionEventTap, e)
def key(code):
    for d in (True, False):
        Q.CGEventPost(Q.kCGSessionEventTap, Q.CGEventCreateKeyboardEvent(None, code, d)); time.sleep(0.02)
for step in sys.argv[1].split(","):
    {"D": lambda: mod(True), "U": lambda: mod(False), "K": lambda: key(124)}[step](); time.sleep(0.1)
'''
def post(seq):
    subprocess.run([sys.executable, "-c", POSTER, seq], check=True); time.sleep(0.3)

def check(name, expected):
    ok = got == expected
    print(("PASS " if ok else "FAIL ") + name, "" if ok else f"got {got}")
    got.clear(); return ok

results = []
post("D,U");      results.append(check("press/release", [("press","hotkey"),("release","hotkey")]))
post("D,K,U");    results.append(check("other key while held", [("press","hotkey"),("press","other"),("release","hotkey")]))
Q.CGEventTapEnable(tap._tap, False)   # simulate macOS disabling the tap
tap.ensure_healthy()
post("D,U");      results.append(check("recovers after macOS disables tap", [("press","hotkey"),("release","hotkey")]))
print("ALL PASS" if all(results) else "SOME FAILED")
