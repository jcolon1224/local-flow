"""Global hotkey listener built directly on a macOS event tap.

Replaces pynput's listener, which has two failure modes that silently kill
dictation: it never re-enables the tap after macOS disables it (which macOS
does whenever a tap is slow, e.g. under memory pressure or after sleep), and
any exception in a callback stops it for good.

Here the tap callback only reads the event and queues it; all real work runs
on a separate dispatch thread, and a watchdog re-enables or rebuilds the tap.
"""
import logging
import os
import queue
import threading
import time

import Quartz as Q

log = logging.getLogger("local-flow")

# Hotkeys that are modifier keys arrive as "flags changed" events. The
# device-dependent flag bit tells left and right apart.
MODIFIERS = {  # name: (keycode, device flag bit)
    "alt_r": (61, 0x40), "alt_l": (58, 0x20),
    "cmd_r": (54, 0x10), "cmd_l": (55, 0x08),
    "ctrl_r": (62, 0x2000), "ctrl_l": (59, 0x01),
    "shift_r": (60, 0x04), "shift_l": (56, 0x02),
    "fn": (63, 0x800000),
}
# Regular keys arrive as key down/up events.
KEYS = {"f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64,
        "f18": 79, "f19": 80, "f5": 96, "f6": 97, "f7": 98, "f8": 100}
ESC = 53


class HotkeyTap:
    """Calls on_event(kind, key, ts): kind is "press"/"release", key is
    "hotkey", "esc" or "other" (any other key press)."""

    def __init__(self, hotkey: str, on_event):
        if hotkey in MODIFIERS:
            self.keycode, self.flag = MODIFIERS[hotkey]
        elif hotkey in KEYS:
            self.keycode, self.flag = KEYS[hotkey], None
        else:
            raise ValueError(f"Unsupported hotkey {hotkey!r}; use one of "
                             f"{', '.join([*MODIFIERS, *KEYS])}")
        self.on_event = on_event
        self.events: queue.Queue = queue.Queue()
        self._tap = None
        self._loop = None
        self._thread = None
        self._down = False
        self._callback_ref = self._callback  # keep alive for the C side
        threading.Thread(target=self._dispatch, daemon=True, name="hotkey-dispatch").start()
        self._start_tap()

    # ----- tap thread -----

    def _start_tap(self):
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True, name="hotkey-tap")
        self._thread.start()
        ready.wait(5)

    def _run(self, ready: threading.Event):
        mask = (Q.CGEventMaskBit(Q.kCGEventKeyDown) | Q.CGEventMaskBit(Q.kCGEventKeyUp)
                | Q.CGEventMaskBit(Q.kCGEventFlagsChanged))
        self._tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap,
                                       Q.kCGEventTapOptionListenOnly, mask,
                                       self._callback_ref, None)
        if self._tap is None:
            log.error("could not create event tap (Input Monitoring / Accessibility missing?)")
            ready.set()
            return
        source = Q.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._loop = Q.CFRunLoopGetCurrent()
        Q.CFRunLoopAddSource(self._loop, source, Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(self._tap, True)
        log.info("hotkey tap running")
        ready.set()
        Q.CFRunLoopRun()

    def _callback(self, proxy, etype, event, refcon):
        # Keep this fast: macOS disables taps whose callbacks are slow.
        try:
            if etype in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
                log.warning("macOS disabled the hotkey tap (%s) — re-enabling", etype)
                Q.CGEventTapEnable(self._tap, True)
                return event
            # Ignore the ⌘V we synthesize when pasting.
            if Q.CGEventGetIntegerValueField(event, Q.kCGEventSourceUnixProcessID) == os.getpid():
                return event
            code = Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventKeycode)
            now = time.time()
            if etype == Q.kCGEventFlagsChanged:
                if self.flag is not None and code == self.keycode:
                    down = bool(Q.CGEventGetFlags(event) & self.flag)
                    self._set_down(down, now)
            elif etype == Q.kCGEventKeyDown:
                if self.flag is None and code == self.keycode:
                    self._set_down(True, now)
                elif not Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventAutorepeat):
                    self.events.put(("press", "esc" if code == ESC else "other", now))
            elif etype == Q.kCGEventKeyUp and self.flag is None and code == self.keycode:
                self._set_down(False, now)
        except Exception:
            log.exception("hotkey tap callback failed")
        return event

    def _set_down(self, down: bool, ts: float):
        if down != self._down:  # also swallows key-repeat
            self._down = down
            self.events.put(("press" if down else "release", "hotkey", ts))

    # ----- dispatch thread -----

    def _dispatch(self):
        while True:
            kind, key, ts = self.events.get()
            try:
                self.on_event(kind, key, ts)
            except Exception:
                log.exception("hotkey handler failed")

    # ----- watchdog (call periodically) -----

    def ensure_healthy(self):
        if self._tap is not None and self._thread.is_alive():
            if Q.CGEventTapIsEnabled(self._tap):
                return
            log.warning("hotkey tap found disabled — re-enabling")
            Q.CGEventTapEnable(self._tap, True)
            if Q.CGEventTapIsEnabled(self._tap):
                return
        log.warning("rebuilding hotkey tap")
        if self._loop is not None:
            Q.CFRunLoopStop(self._loop)
        if self._tap is not None:
            Q.CFMachPortInvalidate(self._tap)
        self._tap = self._loop = None
        self._down = False
        self._start_tap()
