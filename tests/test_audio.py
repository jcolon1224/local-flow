"""Live test of audio.py (needs a real mic; run from Terminal):
repeated start/stop timing, 16 kHz output, device selection, and the
silent-device fallback using a known-silent virtual device if one exists."""
import logging
import sys
import time
from pathlib import Path

import struct

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
import audio  # noqa: E402

results = []


def check(name, cond):
    results.append(cond)
    print(("PASS " if cond else "FAIL ") + name)


devices = audio.input_devices()
print("devices:", [(d.name, "builtin" if d.builtin else "") for d in devices],
      "| default:", (audio.default_input() or audio.InputDevice(0, "?", False)).name)
check("found at least one input device", bool(devices))
check("found the built-in mic", audio.builtin_mic() is not None)

def mic_in_use(device) -> bool:
    """CoreAudio's 'is running somewhere' flag: what drives the orange mic dot."""
    raw = audio._property(device.id, "gone")
    return bool(raw) and struct.unpack("<I", raw)[0] != 0


# 1. Repeated recordings: timing and output format.
r = audio.Recorder()
r.prewarm()
builtin = audio.builtin_mic()
check("a prepared-but-idle engine does not hold the mic open", not mic_in_use(builtin))
starts, stops = [], []
for i in range(5):
    t = time.time(); r.start(); starts.append(time.time() - t)
    time.sleep(0.6)
    if i == 0:
        check("mic is in use while recording", mic_in_use(builtin))
    t = time.time(); a = r.stop(); stops.append(time.time() - t)
    r._settle(2.0)
    time.sleep(0.3)  # let the background prewarm finish
check("mic is released after stop", not mic_in_use(builtin))
print(f"start {1000 * max(starts):.0f} ms max (first {1000 * starts[0]:.0f}), stop {1000 * max(stops):.0f} ms max")
check("start with a prebuilt engine is fast (< 150 ms)", max(starts) < 0.15)
check("stop returns instantly (< 50 ms)", max(stops) < 0.05)
check(f"output is ~0.6 s at 16 kHz (got {len(a) / 16000:.2f} s)", 0.45 < len(a) / 16000 < 0.8)
check("audio is real (not digital silence)", bool(np.any(a)) and a.dtype == np.float32)
check("engine torn down cleanly (not wedged)", not r.wedged and r.settled())

# 2. Explicit device selection.
r2 = audio.Recorder(preferred=lambda: builtin.name)
r2.start(); time.sleep(0.4); b = r2.stop(); r2._settle(2.0)
check(f"explicit selection used {builtin.name!r}", r2.device_name == builtin.name and len(b) > 0)

# 3. Silent-device fallback (only if a known-silent virtual device is present).
silent = next((d for d in devices if "Teams" in d.name or "Zoom" in d.name), None)
if silent:
    r3 = audio.Recorder(preferred=lambda: silent.name)
    r3.start(); time.sleep(1.5); c = r3.stop(); r3._settle(2.0)
    check(f"silent {silent.name!r} fell back to {r3.device_name!r} with audio",
          r3.device_name == builtin.name and len(c) > 8000)
    r3.start(); time.sleep(0.4); r3.stop(); r3._settle(2.0)
    check("second recording skips the silent device immediately", r3.device_name == builtin.name)
else:
    print("skip: no silent virtual device to test fallback with")

print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
