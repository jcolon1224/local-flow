"""Regression test for the CoreAudio deadlock: if stopping the mic hangs forever,
Recorder.stop() must still return the captured audio immediately, and the
recorder must flag itself as wedged so the app restarts. No real mic needed."""
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
import flow  # noqa: E402


class HungStream:
    """Stands in for a PortAudio stream whose stop() never returns."""
    def stop(self):
        threading.Event().wait()

    def close(self):
        pass


r = flow.Recorder()
r.HANG_TIMEOUT = 1.0
r._stream = HungStream()
r._capturing = True
for _ in range(10):
    r._on_audio(np.full((1600, 1), 0.1, dtype=np.float32))

t0 = time.time()
audio = r.stop()
took = time.time() - t0
ok = True
def check(name, cond):
    global ok
    ok = ok and cond
    print(("PASS " if cond else "FAIL ") + name)

check(f"stop() returned immediately ({took * 1000:.0f} ms) despite hung audio", took < 0.5)
check("captured audio is intact (1.0s)", len(audio) == 16000)
check("not wedged yet", not r.wedged)
time.sleep(1.5)
check("flagged as wedged after the timeout", r.wedged)
try:
    r.start()
    check("start() refuses while wedged", False)
except RuntimeError:
    check("start() refuses while wedged (app restarts instead)", True)
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
