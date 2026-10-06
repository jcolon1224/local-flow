"""Microphone capture on Apple's AVAudioEngine — no PortAudio.

Why not sounddevice/PortAudio: its stop-stream call deadlocks inside CoreAudio
every few dictations on recent macOS (the stop thread and the audio I/O thread
each wait for a lock the other holds), which froze the app until it restarted
itself. AVAudioEngine is the API macOS apps are expected to use; stopping it
takes milliseconds.

Device list and selection go through CoreAudio directly (ctypes), so there is
no device cache to refresh: every call sees the mics that exist right now.
"""
import ctypes
import logging
import struct
import threading
import time
import warnings
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable

import numpy as np
import objc
from AVFoundation import AVAudioEngine
from scipy.signal import resample_poly

log = logging.getLogger("local-flow")
TARGET_RATE = 16000  # what Whisper expects

warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)

# ---------------------------------------------------------------- CoreAudio HAL

_ca = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_at = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox")


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


_ca.AudioObjectGetPropertyDataSize.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
_ca.AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
_cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFRelease.argtypes = [ctypes.c_void_p]
_at.AudioUnitSetProperty.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
_at.AudioUnitSetProperty.restype = ctypes.c_int32

_SYSTEM_OBJECT = 1
_CURRENT_DEVICE = 2000  # kAudioOutputUnitProperty_CurrentDevice
_UTF8 = 0x08000100


def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode())[0]


def _property(obj: int, selector: str, scope: str = "glob") -> bytes | None:
    addr = _Address(_fourcc(selector), _fourcc(scope), 0)
    size = ctypes.c_uint32(0)
    if _ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)) or not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf):
        return None
    return buf.raw[: size.value]


def _cfstring(raw: bytes | None) -> str:
    if not raw:
        return ""
    ref = ctypes.c_void_p.from_buffer_copy(raw).value
    out = ctypes.create_string_buffer(512)
    ok = _cf.CFStringGetCString(ref, out, 512, _UTF8)
    _cf.CFRelease(ref)
    return out.value.decode("utf-8", "replace") if ok else ""


@dataclass(frozen=True)
class InputDevice:
    id: int
    name: str
    builtin: bool


def input_devices() -> list[InputDevice]:
    """Every device with input channels, as CoreAudio sees them right now."""
    raw = _property(_SYSTEM_OBJECT, "dev#")
    if not raw:
        return []
    devices = []
    for dev_id in struct.unpack(f"<{len(raw) // 4}I", raw):
        layout = _property(dev_id, "slay", "inpt")  # kAudioDevicePropertyStreamConfiguration
        if not layout:
            continue
        nbuf = struct.unpack("<I", layout[:4])[0]
        channels = sum(struct.unpack("<I", layout[8 + i * 16: 12 + i * 16])[0] for i in range(nbuf))
        if not channels:
            continue
        transport = _property(dev_id, "tran")
        builtin = bool(transport) and struct.unpack("<I", transport)[0] == _fourcc("bltn")
        devices.append(InputDevice(dev_id, _cfstring(_property(dev_id, "lnam")), builtin))
    return devices


def default_input() -> InputDevice | None:
    raw = _property(_SYSTEM_OBJECT, "dIn ")
    if not raw:
        return None
    dev_id = struct.unpack("<I", raw)[0]
    return next((d for d in input_devices() if d.id == dev_id), None)


def builtin_mic() -> InputDevice | None:
    return next((d for d in input_devices() if d.builtin), None)


def device_named(name: str) -> InputDevice | None:
    return next((d for d in input_devices() if d.name == name), None)


# ---------------------------------------------------------------- recorder

class Recorder:
    """Records from the preferred mic (or the system default), falling back to
    the built-in mic if the chosen one delivers nothing — the iPhone Continuity
    mic, Teams/Zoom virtual devices and disconnected headsets often open fine
    but stay silent.

    `preferred` returns the configured device name, or "auto".
    Audio is returned as float32 at 16 kHz.
    """

    FALLBACK_AFTER = 0.5  # seconds without any audio before switching mics
    HANG_TIMEOUT = 3.0    # a stop slower than this is treated as hung

    def __init__(self, preferred: Callable[[], str] = lambda: "auto"):
        self._preferred = preferred
        self._engine = None
        self._node = None
        self._rate = TARGET_RATE
        self._chunks: list[np.ndarray] = []
        self._capturing = False
        self._lock = threading.Lock()
        self._pending: threading.Thread | None = None  # in-flight stop
        self.wedged = False  # audio system hung; the app should restart
        self.device_name = ""
        self.silent_devices: set[str] = set()  # skipped until the list is refreshed
        self.level = 0.0  # current loudness 0..1, for the floating pill
        self._tap_ref = self._tap  # keep the block callable alive
        # Creating an AVAudioEngine costs 100-250 ms; starting a prepared one
        # costs ~50 ms. So the next engine is built ahead of time, off the
        # hotkey thread, and start() only has to switch it on.
        self._spare: tuple | None = None  # (engine, node, rate, stride, device)

    # ----- guards -----

    def _acquire(self):
        if not self._lock.acquire(timeout=self.HANG_TIMEOUT):
            self.wedged = True
            raise RuntimeError("audio system is not responding")

    def _background(self, fn, what: str):
        """Run something that may hang off the caller's thread, with a watchdog."""
        def run():
            try:
                fn()
            except Exception:
                log.exception(what)

        worker = threading.Thread(target=run, daemon=True, name="audio-" + what)
        worker.start()
        self._pending = worker

        def watch():
            worker.join(self.HANG_TIMEOUT)
            if worker.is_alive():
                log.error("audio system hung during %s — the app will restart", what)
                self.wedged = True

        threading.Thread(target=watch, daemon=True).start()

    def _settle(self, seconds: float) -> bool:
        pending = self._pending
        if pending is not None and pending.is_alive():
            pending.join(seconds)
            return not pending.is_alive()
        return True

    def settled(self) -> bool:
        return self._settle(0)

    # ----- recording -----

    def start(self):
        self._acquire()
        try:
            if self.wedged or not self._settle(1.0):
                self.wedged = True
                raise RuntimeError("audio system is restarting — try again in a few seconds")
            self._chunks = []
            self._capturing = True
            device = self._pick()
            try:
                self._open(device)
            except RuntimeError:
                if device is None:
                    raise
                log.warning("couldn't open %s — using the system default mic", device.name)
                self._open(None)
        finally:
            self._lock.release()
        threading.Thread(target=self._verify_audio, daemon=True).start()

    def _pick(self) -> InputDevice | None:
        """The device to open, or None for the system default."""
        wanted = self._preferred()
        device = None
        if wanted != "auto":
            device = device_named(wanted)
            if device is None:
                log.warning("mic %r not connected — using system default", wanted)
        current = device or default_input()
        if current is not None and current.name in self.silent_devices:
            return builtin_mic() or device
        return device

    def _build(self, device: InputDevice | None) -> tuple:
        """Create and prepare (but don't start) an engine for `device`."""
        engine = AVAudioEngine.alloc().init()
        node = engine.inputNode()
        if device is not None:
            unit = node.audioUnit()
            dev_id = ctypes.c_uint32(device.id)
            rc = _at.AudioUnitSetProperty(unit.pointerAsInteger, _CURRENT_DEVICE, 0, 0,
                                          ctypes.byref(dev_id), 4)
            if rc != 0:
                raise RuntimeError(f"couldn't select mic {device.name!r} (CoreAudio error {rc})")
        fmt = node.outputFormatForBus_(0)
        rate, channels = int(fmt.sampleRate()), int(fmt.channelCount())
        if rate <= 0 or channels <= 0:
            raise RuntimeError("the microphone reports no audio format (unplugged?)")
        stride = channels if fmt.isInterleaved() else 1
        node.installTapOnBus_bufferSize_format_block_(0, 2048, fmt, self._tap_ref)
        engine.prepare()
        return engine, node, rate, stride, device

    def _take_spare(self, device: InputDevice | None) -> tuple | None:
        spare, self._spare = self._spare, None
        if spare is None:
            return None
        if spare[4] != device:  # built for a different mic (or it vanished)
            spare[1].removeTapOnBus_(0)
            return None
        return spare

    def _open(self, device: InputDevice | None):
        built = self._take_spare(device)
        prebuilt = built is not None
        if built is None:
            built = self._build(device)
        engine, node, rate, stride, _ = built
        ok, err = engine.startAndReturnError_(None)
        if not ok and prebuilt:  # the prepared engine went stale; build fresh
            node.removeTapOnBus_(0)
            engine, node, rate, stride, _ = self._build(device)
            ok, err = engine.startAndReturnError_(None)
        if not ok:
            node.removeTapOnBus_(0)
            raise RuntimeError(f"couldn't start the microphone: {err}")
        self._engine, self._node, self._rate, self._interleaved_channels = engine, node, rate, stride
        current = device or default_input()
        self.device_name = current.name if current else "default mic"
        log.info("recording from %s (%d Hz%s)", self.device_name, rate, ", prebuilt" if prebuilt else "")

    def prewarm(self):
        """Build the next engine ahead of time. Safe to call from any thread;
        skipped if a recording is in progress."""
        if self.wedged or not self._lock.acquire(timeout=0.5):
            return
        try:
            if self._engine is not None or self._spare is not None:
                return
            device = self._pick()
            try:
                self._spare = self._build(device)
            except Exception:
                log.exception("prewarming audio engine")
        finally:
            self._lock.release()

    def discard_spare(self):
        """Drop the prebuilt engine (e.g. after wake or a mic change)."""
        if self._lock.acquire(timeout=0.5):
            try:
                self._take_spare(object())  # never matches: always discards
            finally:
                self._lock.release()

    def _tap(self, buffer, _when):
        # Runs on the audio thread: copy out and leave.
        if not self._capturing:
            return
        n = int(buffer.frameLength())
        if n <= 0:
            return
        try:
            stride = self._interleaved_channels
            raw = buffer.floatChannelData()[0].as_buffer(n * stride * 4)
            samples = np.frombuffer(raw, dtype=np.float32, count=n * stride)
            chunk = samples[::stride].copy() if stride > 1 else samples.copy()
        except Exception:
            log.exception("reading audio buffer")
            return
        self._chunks.append(chunk)
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        # -55 dB (room noise) .. -15 dB (loud speech) -> 0..1
        self.level = min(1.0, max(0.0, (20 * np.log10(rms + 1e-7) + 55) / 40))

    def _verify_audio(self):
        time.sleep(self.FALLBACK_AFTER)
        if not self._lock.acquire(timeout=self.HANG_TIMEOUT):
            return
        try:
            if self._engine is None or not self._capturing:
                return
            # Real mics always pick up some noise; exact zeros mean a virtual or
            # idle device (Teams/Zoom audio, a Continuity iPhone that isn't sending).
            if self._chunks and any(np.any(c) for c in self._chunks):
                return
            fallback = builtin_mic()
            if fallback is None or fallback.name == self.device_name:
                return
            log.warning("no audio from %s — switching to %s", self.device_name, fallback.name)
            self.silent_devices.add(self.device_name)
            self._close()
            if not self._settle(1.0):
                return
            try:
                self._open(fallback)
            except RuntimeError:
                log.exception("built-in mic fallback failed")
        finally:
            self._lock.release()

    def _close(self):
        """Detach the engine and stop it in the background (never blocks)."""
        engine, node, self._engine, self._node = self._engine, self._node, None, None
        if engine is None:
            return

        def stop():
            engine.stop()
            node.removeTapOnBus_(0)
            self.prewarm()  # have the next one ready before the next key press

        self._background(stop, "mic stop")

    @property
    def active(self) -> bool:
        return self._engine is not None

    def seconds(self) -> float:
        return sum(len(c) for c in self._chunks) / self._rate

    def stop(self) -> np.ndarray:
        """Returns the captured audio at 16 kHz. The audio is already in memory,
        so this returns immediately even if stopping the engine hangs."""
        self._capturing = False
        chunks, self._chunks = self._chunks, []
        rate = self._rate
        try:
            self._acquire()
        except RuntimeError:
            log.error("recorder lock stuck; returning captured audio anyway")
        else:
            try:
                self._close()
            finally:
                self._lock.release()
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(chunks)
        if rate != TARGET_RATE:
            ratio = Fraction(TARGET_RATE, rate)
            audio = resample_poly(audio, ratio.numerator, ratio.denominator).astype(np.float32)
        return audio

    def refresh_devices(self):
        """Forget which mics were silent so they get another chance, and rebuild
        the prepared engine so it reflects the mics that exist now."""
        self.silent_devices.clear()
        self.discard_spare()
        threading.Thread(target=self.prewarm, daemon=True, name="audio-prewarm").start()
