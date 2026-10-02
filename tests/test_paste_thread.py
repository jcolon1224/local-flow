"""Regression test for the macOS 26 SIGTRAP: building the ⌘V events must be safe
off the main thread. Creates the events but does not post them (nothing is pasted)."""
import threading
import Quartz

results = []

def build():
    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(source, 9, down)
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        results.append(event is not None)

t = threading.Thread(target=build)
t.start(); t.join()
print("PASS: background-thread key events OK" if results and all(results) else "FAIL")
