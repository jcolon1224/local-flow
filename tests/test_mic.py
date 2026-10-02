"""Live test: a silent mic (Teams virtual device) must fall back to the built-in mic."""
import logging, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
import flow

flow.CONFIG["microphone"] = "Microsoft Teams Audio"
r = flow.Recorder()
r.start(); time.sleep(2.0); a = r.stop()
print(f"captured {len(a)/16000:.2f}s from {r.device_name!r}")
ok = len(a) > 16000 * 1.0 and "MacBook" in r.device_name
flow.CONFIG["microphone"] = "auto"
r.start(); time.sleep(1.0); b = r.stop()
print(f"auto: captured {len(b)/16000:.2f}s from {r.device_name!r}")
ok = ok and len(b) > 16000 * 0.8
print("PASS" if ok else "FAIL")
