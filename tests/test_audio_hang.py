"""Regression test: if stopping the audio engine ever hangs, Recorder.stop() must
still return the captured audio immediately, and the recorder must flag itself
as wedged so the app restarts. No real mic needed."""
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
import audio  # noqa: E402


class HungEngine:
    """Stands in for an AVAudioEngine whose stop() never returns."""
    def stop(self):
        threading.Event().wait()


class DummyNode:
    def removeTapOnBus_(self, bus):
        pass


r = audio.Recorder()
r.HANG_TIMEOUT = 1.0
r._engine, r._node, r._rate = HungEngine(), DummyNode(), 16000
r._capturing = True
for _ in range(10):
    r._chunks.append(np.full(1600, 0.1, dtype=np.float32))

t0 = time.time()
captured = r.stop()
took = time.time() - t0
ok = True


def check(name, cond):
    global ok
    ok = ok and cond
    print(("PASS " if cond else "FAIL ") + name)


check(f"stop() returned immediately ({took * 1000:.0f} ms) despite a hung engine", took < 0.5)
check("captured audio is intact (1.0 s)", len(captured) == 16000)
check("not wedged yet", not r.wedged)
time.sleep(1.5)
check("flagged as wedged after the timeout", r.wedged)
try:
    r.start()
    check("start() refuses while wedged", False)
except RuntimeError:
    check("start() refuses while wedged (app restarts instead)", True)

# Resampling path: 48 kHz in -> 16 kHz out, length preserved in seconds.
r2 = audio.Recorder()
r2._rate = 48000
r2._chunks = [np.sin(np.linspace(0, 200, 48000)).astype(np.float32)]
out = r2.stop()
check(f"48 kHz audio is resampled to 16 kHz (1.0 s -> {len(out)} samples)", len(out) == 16000)

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
