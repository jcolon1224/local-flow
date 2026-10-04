"""Local Flow: hold a hotkey, speak, release -> text is pasted at your cursor.

Speech-to-text runs on-device (Whisper via Apple's MLX). Cleanup runs locally
by default (rules.py); the optional Claude pass (cleanup.py) sends the
transcript text — never audio — to Claude Haiku.

Controls:
  hold hotkey            push-to-talk
  double-tap hotkey      hands-free: keeps recording until you tap it again
  Esc (hands-free)       cancel without pasting
"""
import ctypes
import ctypes.util
import fcntl
import json
import logging
import os
import queue
import sys
import subprocess
import threading
import time
from pathlib import Path

# The speech model is downloaded once; after that start fully offline so the app
# works at login before Wi-Fi connects (see _load_model for the fallback).
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np
import rumps
import sounddevice as sd
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory,
                    NSPasteboard, NSPasteboardItem, NSPasteboardTypeString)
from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
from Foundation import (NSActivityLatencyCritical, NSActivityUserInitiatedAllowingIdleSystemSleep,
                        NSProcessInfo)
import Quartz

from cleanup import DICTIONARY, SNIPPETS, Cleaner, dictionary_words
from history_view import open_history as open_history_page
from hotkey import HotkeyTap
from overlay import RecordingPill
from styles import STYLES_PATH, Target, apply_style, current_target, load_styles

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
HISTORY = ROOT / "history.jsonl"
LOG = ROOT / "local-flow.log"

DEFAULTS = {
    "hotkey": "alt_r",
    "model": "models/whisper-small.en-q8",
    "wired_mb": 450,
    "language": "en",
    "local_cleanup": True,
    "claude_cleanup": False,
    "sounds": True,
    "restore_clipboard": True,
    "max_minutes": 10,
    "microphone": "auto",
    "overlay": True,
    "app_styles": True,
}
# Personal settings files are not in git; create them from the examples on first run.
for _name in ("config", "dictionary", "snippets"):
    _path, _example = ROOT / f"{_name}.json", ROOT / f"{_name}.example.json"
    if not _path.exists() and _example.exists():
        _path.write_text(_example.read_text())
CONFIG = {**DEFAULTS, **json.loads(CONFIG_PATH.read_text())}

SAMPLE_RATE = 16000  # what Whisper expects
TAP_SECONDS = 0.3  # a press shorter than this is a "tap", not push-to-talk
DOUBLE_TAP_WINDOW = 0.45
KEEP_WARM_SECONDS = 90  # touch the model periodically so macOS doesn't page it out
WATCHDOG_SECONDS = 3
# Whisper invents these on near-silent audio.
HALLUCINATIONS = {"thank you", "thanks for watching", "thank you for watching",
                  "you", "bye", "thank you so much", "subtitles by the amara.org community"}

ICON_IDLE, ICON_REC, ICON_HANDSFREE, ICON_BUSY, ICON_WARN = "🎙", "🔴", "🟠", "⏳", "⚠️"

logging.basicConfig(filename=LOG, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("local-flow")


def save_config():
    CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2) + "\n")


def play(sound: str):
    if CONFIG["sounds"]:
        subprocess.Popen(["afplay", "-v", "0.25", f"/System/Library/Sounds/{sound}.aiff"])


# ---------- transcription ----------

class Transcriber:
    def __init__(self, model: str, language: str):
        import mlx.core as mx
        import mlx_whisper  # heavy import; keep out of module load

        self._mw = mlx_whisper
        # Local model folders (e.g. the 8-bit copy from quantize_model.py) are
        # relative to this project; anything else is a Hugging Face repo id.
        local = ROOT / model
        if local.exists():
            self.model = str(local)
        elif model.startswith("models/"):  # local copy missing: use the original
            self.model = "mlx-community/whisper-small.en-mlx"
        else:
            self.model = model
        # Keep the model's memory wired so macOS can't swap it to disk — on an
        # 8 GB Mac under memory pressure, paging it back in cost 5-8 seconds.
        if CONFIG["wired_mb"]:
            mx.set_wired_limit(CONFIG["wired_mb"] * 1024 * 1024)
        # MLX otherwise keeps ~800 MB of scratch buffers after each run, which on
        # an 8 GB Mac pushes everything (including this model) into swap.
        mx.set_cache_limit(64 * 1024 * 1024)
        self.language = language
        self._lock = threading.Lock()  # keep-warm and real dictations never overlap

    def prefetch(self):
        """Called when the hotkey goes down: page the model back in while the
        user is still talking. Skipped if a transcription is already running."""
        if self._lock.acquire(blocking=False):
            try:
                self._transcribe(np.zeros(SAMPLE_RATE // 4, dtype=np.float32))
            finally:
                self._lock.release()

    def warm_up(self):
        # Loads weights (and downloads them on first run) so the first real
        # dictation isn't slow.
        self.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        with self._lock:
            return self._transcribe(audio)

    def _transcribe(self, audio: np.ndarray) -> str:
        result = self._mw.transcribe(
            audio,
            path_or_hf_repo=self.model,
            language=self.language,
            # Biases Whisper toward spelling your dictionary words correctly.
            initial_prompt=", ".join(dictionary_words()) or None,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        return result["text"].strip()


def voiced_seconds(audio: np.ndarray) -> float:
    """Rough amount of actual speech, from 30ms frames above a noise floor."""
    frame = int(SAMPLE_RATE * 0.03)
    n = len(audio) // frame
    if n == 0:
        return 0.0
    rms = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1))
    floor = max(0.006, np.percentile(rms, 20) * 2.5)
    return float(np.sum(rms > floor)) * 0.03


# ---------- recording ----------

class Recorder:
    """Records from the chosen mic, falling back to the built-in mic if the
    chosen one delivers nothing (iPhone Continuity mic, Teams/Zoom virtual
    devices and disconnected headsets often "open" fine but stay silent).

    PortAudio calls that stop or re-scan audio can deadlock inside CoreAudio
    (seen on the first recording after wake from sleep), so they never run on
    the caller's thread: they run on helper threads with a timeout, and a hang
    sets `wedged` so the app can restart itself instead of freezing.
    """

    FALLBACK_AFTER = 0.5  # seconds without any audio before switching mics
    HANG_TIMEOUT = 3.0    # a PortAudio call slower than this is considered hung

    def __init__(self):
        self._chunks: list[np.ndarray] = []
        self._stream = None
        self._lock = threading.Lock()
        self._capturing = False
        self._pending: threading.Thread | None = None  # in-flight stop/re-scan
        self.wedged = False  # audio system hung; the app must restart
        self.device_name = ""
        self.silent_devices: set[str] = set()  # skipped until "Refresh list"
        self.level = 0.0  # current mic loudness, 0..1, for the floating pill

    # ----- guarded PortAudio helpers -----

    def _acquire(self):
        if not self._lock.acquire(timeout=self.HANG_TIMEOUT):
            self.wedged = True
            raise RuntimeError("audio system is not responding")

    def _background(self, fn, what: str):
        """Run a PortAudio call that may hang, off the caller's thread."""
        def run():
            try:
                fn()
            except sd.PortAudioError:
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
        """Wait briefly for an in-flight stop/re-scan; False if it's still running."""
        pending = self._pending
        if pending is not None and pending.is_alive():
            pending.join(seconds)
            return not pending.is_alive()
        return True

    # ----- recording -----

    def start(self):
        self._acquire()
        try:
            if self.wedged or not self._settle(1.0):
                self.wedged = True
                raise RuntimeError("audio system is restarting — try again in a few seconds")
            self._chunks = []
            self._capturing = True
            try:
                self._open(self._pick())
            except sd.PortAudioError:
                # Mic list is stale (e.g. AirPods connected/disconnected): reload and retry.
                refresh_audio_devices()
                self._open(self._pick())
        finally:
            self._lock.release()
        threading.Thread(target=self._verify_audio, daemon=True).start()

    def _pick(self) -> int | None:
        device = choose_input_device()
        info = sd.query_devices(device) if device is not None else sd.query_devices(kind="input")
        name = info["name"]
        if name in self.silent_devices:
            return builtin_mic_index()
        return device

    def _open(self, device):
        self._stream = sd.InputStream(
            device=device, samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            callback=self._on_audio,
        )
        self._stream.start()
        self.device_name = sd.query_devices(device, kind="input")["name"] if device is not None \
            else sd.query_devices(kind="input")["name"]
        log.info("recording from %s", self.device_name)

    def _on_audio(self, data, *_):
        if not self._capturing:
            return
        self._chunks.append(data.copy())
        rms = float(np.sqrt(np.mean(data ** 2)))
        # -55 dB (room noise) .. -15 dB (loud speech) -> 0..1
        self.level = min(1.0, max(0.0, (20 * np.log10(rms + 1e-7) + 55) / 40))

    def _verify_audio(self):
        time.sleep(self.FALLBACK_AFTER)
        if not self._lock.acquire(timeout=self.HANG_TIMEOUT):
            return
        try:
            if self._stream is None:
                return
            # Real mics always pick up some noise; exact zeros mean a virtual or
            # idle device (Teams/Zoom audio, a Continuity iPhone that isn't sending).
            if self._chunks and any(np.any(c) for c in self._chunks):
                return
            builtin = builtin_mic_index()
            if builtin is None or self.device_name == sd.query_devices(builtin)["name"]:
                return
            log.warning("no audio from %s — switching to built-in mic", self.device_name)
            self.silent_devices.add(self.device_name)
            self._close()
            if not self._settle(1.0):
                return
            try:
                self._open(builtin)
            except sd.PortAudioError:
                log.exception("built-in mic fallback failed")
        finally:
            self._lock.release()

    def _close(self):
        """Detach the stream and stop it in the background (never blocks)."""
        stream, self._stream = self._stream, None
        if stream is None:
            return

        def stop_and_close():
            stream.stop()
            stream.close()

        self._background(stop_and_close, "mic stop")

    @property
    def active(self) -> bool:
        return self._stream is not None

    def seconds(self) -> float:
        return sum(len(c) for c in self._chunks) / SAMPLE_RATE

    def stop(self) -> np.ndarray:
        """Returns what was captured. The audio is already in memory, so this
        returns immediately even if the audio system hangs while stopping."""
        self._capturing = False
        chunks, self._chunks = self._chunks, []
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
        return np.concatenate(chunks).flatten()

    def settled(self) -> bool:
        """True when no stop/re-scan is in flight (safe to list devices)."""
        return self._settle(0)

    def refresh_devices(self):
        """Re-scan mics in the background, but never while recording or mid-stop."""
        if self.wedged or not self._lock.acquire(timeout=0.2):
            return
        try:
            if self._stream is None and self._settle(0):
                self._background(refresh_audio_devices, "device re-scan")
        finally:
            self._lock.release()


def input_devices() -> list[tuple[int, str]]:
    return [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def builtin_mic_index() -> int | None:
    for i, name in input_devices():
        if "macbook" in name.lower() or "built-in" in name.lower():
            return i
    return None


def choose_input_device() -> int | None:
    """The mic picked in the menu, or None for the system default."""
    wanted = CONFIG.get("microphone", "auto")
    if wanted != "auto":
        for i, name in input_devices():
            if name == wanted:
                return i
        log.warning("mic %r not connected — using system default", wanted)
    return None


# ---------- permissions ----------

_IOKIT = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
_LISTEN_EVENT = 1  # kIOHIDRequestTypeListenEvent (Input Monitoring)
SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_{}"


def missing_permissions(prompt: bool = False) -> list[str]:
    """Which of the hotkey/paste permissions macOS hasn't granted yet.

    With prompt=True, macOS shows its permission dialogs and adds Local Flow
    to the lists in System Settings, so the user only has to flip the switch.
    """
    missing = []
    mic = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
    if mic == 0 and prompt:  # never asked: show the macOS Microphone prompt
        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeAudio, lambda granted: log.info("microphone prompt answered: %s", granted))
    elif mic in (1, 2):  # restricted / denied
        missing.append("Microphone")
    if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}):
        missing.append("Accessibility")
    if prompt:
        _IOKIT.IOHIDRequestAccess(_LISTEN_EVENT)
    if _IOKIT.IOHIDCheckAccess(_LISTEN_EVENT) != 0:  # 0 = granted
        missing.append("Input Monitoring")
    return missing


def restart():
    """Re-exec ourselves: the hotkey listener only works if it starts *after* access is granted."""
    log.info("restarting")
    os.execv(sys.executable, [sys.executable, str(ROOT / "flow.py")])


def refresh_audio_devices():
    """PortAudio snapshots devices at startup; re-scan to see new mics/headsets."""
    sd._terminate()
    sd._initialize()


# ---------- output ----------

def _snapshot(pb) -> list[dict]:
    items = []
    for item in pb.pasteboardItems() or []:
        items.append({t: item.dataForType_(t) for t in item.types()
                      if item.dataForType_(t) is not None})
    return items


def _restore(pb, items: list[dict]):
    pb.clearContents()
    objs = []
    for data in items:
        obj = NSPasteboardItem.alloc().init()
        for t, d in data.items():
            obj.setData_forType_(d, t)
        objs.append(obj)
    if objs:
        pb.writeObjects_(objs)


V_KEYCODE = 9  # the physical V key


def press_cmd_v():
    """Post ⌘V straight to the window server.

    Deliberately not pynput: its Controller looks up the keyboard layout via
    Text Input Sources, which macOS 26 only allows on the main thread — calling
    it from the paste worker kills the app with SIGTRAP.
    """
    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(source, V_KEYCODE, down)
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.01)


def paste(text: str):
    pb = NSPasteboard.generalPasteboard()
    saved = _snapshot(pb) if CONFIG["restore_clipboard"] else None
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)
    ours = pb.changeCount()
    press_cmd_v()
    if saved is not None:
        time.sleep(0.5)  # give the target app time to read the clipboard
        if pb.changeCount() == ours:  # don't clobber something you copied meanwhile
            _restore(pb, saved)


def log_history(raw: str, text: str, seconds: float, stt_s: float, clean_s: float,
                target: "Target"):
    with HISTORY.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "audio_s": round(seconds, 2),
                            "stt_s": round(stt_s, 2), "cleanup_s": round(clean_s, 2),
                            "app": target.name, "style": target.style,
                            "raw": raw, "text": text}) + "\n")


# ---------- app ----------

class FlowApp(rumps.App):
    def __init__(self):
        super().__init__("Local Flow", title=ICON_BUSY, quit_button="Quit Local Flow")
        self.local_item = rumps.MenuItem("Smart cleanup (free, on-device)", callback=self.toggle_local)
        self.claude_item = rumps.MenuItem("Use Claude Haiku instead (paid)", callback=self.toggle_claude)
        self.sounds_item = rumps.MenuItem("Sounds", callback=self.toggle_sounds)
        self.mic_menu = rumps.MenuItem("Microphone")
        self.overlay_item = rumps.MenuItem("Floating indicator", callback=self.toggle_overlay)
        self.styles_item = rumps.MenuItem("Per-app styles", callback=self.toggle_styles)
        self.menu = ["Loading speech model…", None,
                     "Search history…", None,
                     self.local_item, self.claude_item, self.styles_item,
                     self.overlay_item, self.sounds_item, self.mic_menu, None,
                     "Edit dictionary", "Edit snippets", "Edit app styles", None,
                     "Fix permissions…", "Open log", "Restart Local Flow"]
        self._mics_changed = False
        self._build_mic_menu()
        self.cleaner = Cleaner(CONFIG["local_cleanup"], CONFIG["claude_cleanup"])
        self.local_item.state = CONFIG["local_cleanup"]
        self.claude_item.state = CONFIG["claude_cleanup"]
        self.sounds_item.state = CONFIG["sounds"]
        self.overlay_item.state = CONFIG["overlay"]
        self.styles_item.state = CONFIG["app_styles"]
        load_styles()  # creates styles.json with defaults on first run
        self.pill = RecordingPill()
        self.target = Target("", "", "", "default")

        self.recorder = Recorder()
        self.transcriber = None
        self.lock = threading.Lock()
        self.recording = False
        self.handsfree = False
        self.busy = False  # True while any dictation is waiting/transcribing
        self.jobs: queue.Queue = queue.Queue()  # processed in order by one worker
        self._job_started: float | None = None
        threading.Thread(target=self._worker, daemon=True, name="dictation-worker").start()
        self.press_t = 0.0
        self.last_tap_t = 0.0
        self.hotkey_down = False

        self._icon, self._status_text = ICON_BUSY, None
        self.missing = missing_permissions(prompt=True)
        log.info("missing permissions at start: %s (mic status %s)", self.missing or "none",
                 AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio))
        self._last_perm_check = time.time()
        self._last_watchdog = self._last_warm = time.time()
        # Opt out of App Nap: a throttled background app responds to the hotkey late.
        self._activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            NSActivityUserInitiatedAllowingIdleSystemSleep | NSActivityLatencyCritical,
            "Listening for the dictation hotkey")
        rumps.Timer(self._tick, 0.1).start()
        rumps.Timer(self._pill_tick, 0.05).start()
        threading.Thread(target=self._load_model, daemon=True).start()
        self.hotkeys = HotkeyTap(CONFIG["hotkey"], self._on_key)

    def _load_model(self):
        try:
            self.transcriber = Transcriber(CONFIG["model"], CONFIG["language"])
            try:
                self.transcriber.warm_up()
            except Exception:
                # Model not downloaded yet (first run / changed in config): fetch it.
                log.info("model not cached — downloading")
                import huggingface_hub.constants
                huggingface_hub.constants.HF_HUB_OFFLINE = False
                self.transcriber.warm_up()
            self._set_icon(ICON_IDLE)
            self._show_ready()
            log.info("model ready: %s", CONFIG["model"])
        except Exception as e:
            log.exception("model load failed")
            self._status(f"Model failed to load: {type(e).__name__}")

    def _show_ready(self):
        if self.missing:
            self._status("⚠️ Turn on Local Flow in: " + " & ".join(self.missing))
            self._set_icon(ICON_WARN)
        else:
            self._status(f"Ready — hold {self._hotkey_name()} to dictate")

    def _hotkey_name(self) -> str:
        return {"alt_r": "Right ⌥", "cmd_r": "Right ⌘", "ctrl_r": "Right ⌃"}.get(
            CONFIG["hotkey"], CONFIG["hotkey"])

    # UI state is set from worker threads and applied on the main thread.
    def _set_icon(self, icon: str):
        self._icon = icon

    def _status(self, text: str):
        self._status_text = text

    def _self_heal(self, now: float):
        """Restart instead of staying frozen. Each of these is a state the app
        can't recover from in-process (e.g. a CoreAudio deadlock)."""
        reason = None
        if self.hotkeys.stuck_seconds() > 10:
            reason = "hotkey handler stuck"
        elif self._job_started and now - self._job_started > 180:
            reason = "transcription stuck"
        elif self.recorder.wedged and not self.recording and not self.busy:
            reason = "audio system hung"
        if reason:
            log.error("self-heal: %s — restarting", reason)
            restart()

    def _pill_tick(self, _):
        if not CONFIG["overlay"]:
            self.pill.hide()
        elif self.recording:
            self.pill.show("handsfree" if self.handsfree else "rec")
            self.pill.update(self.recorder.level)
        elif self.busy:
            self.pill.show("busy")
            self.pill.update(0.0)
        else:
            self.pill.hide()

    def _tick(self, _):
        if self.title != self._icon:
            self.title = self._icon
        if self._status_text:
            first = list(self.menu.keys())[0]
            self.menu[first].title = self._status_text
            self._status_text = None
        # Waiting on permissions: as soon as they're granted, restart to pick them up.
        if self.missing and time.time() - self._last_perm_check > 2:
            self._last_perm_check = time.time()
            if not missing_permissions():
                restart()
        # Menus must be rebuilt on the main thread, and not mid re-scan.
        if self._mics_changed and self.recorder.settled():
            self._mics_changed = False
            self._build_mic_menu()
        now = time.time()
        if now - self._last_watchdog > WATCHDOG_SECONDS:
            self._last_watchdog = now
            self.hotkeys.ensure_healthy()
            self._self_heal(now)
        if (self.transcriber and not self.recording and not self.busy
                and now - self._last_warm > KEEP_WARM_SECONDS):
            self._last_warm = now
            threading.Thread(target=self.transcriber.warm_up, daemon=True).start()
        # Safety cap so a forgotten hands-free session doesn't record forever.
        if self.recording and self.recorder.seconds() > CONFIG["max_minutes"] * 60:
            with self.lock:
                if self.recording:
                    self._finish()

    # ----- hotkey state machine -----

    def _on_key(self, kind: str, key: str, ts: float):
        if kind == "press":
            self._on_press(key, ts)
        else:
            self._on_release(key, ts)

    def _on_press(self, key: str, ts: float):
        if key == "hotkey":
            self.hotkey_down = True
            with self.lock:
                if self.handsfree:
                    log.info("hands-free stop")
                    self._finish()  # tap during hands-free = done
                elif not self.recording:
                    self._start(ts)
            return
        with self.lock:
            if self.recording and not self.handsfree:
                # Hotkey used as a modifier (e.g. Option+arrow) — not dictation.
                log.info("cancelled: another key pressed with the hotkey")
                self._cancel(quiet=True)
            elif self.handsfree and key == "esc":
                log.info("hands-free cancelled with Esc")
                self._cancel()

    def _on_release(self, key: str, ts: float):
        if key != "hotkey":
            return
        was_down, self.hotkey_down = self.hotkey_down, False
        with self.lock:
            if not was_down or not self.recording or self.handsfree:
                return
            now = ts
            if now - self.press_t < TAP_SECONDS:
                if now - self.last_tap_t < DOUBLE_TAP_WINDOW:
                    self.handsfree = True  # double-tap: keep recording
                    log.info("hands-free on")
                    self._set_icon(ICON_HANDSFREE)
                    play("Pop")
                    self.last_tap_t = 0.0
                else:
                    self.last_tap_t = now
                    self._cancel(quiet=True)
                return
            self._finish()

    def _start(self, ts: float):
        # Recording is allowed while an earlier dictation is still transcribing;
        # transcriptions queue up on the model lock and paste in order.
        if not self.transcriber:
            self._status("Still loading the speech model…")
            log.info("hotkey pressed before model was ready")
            return
        try:
            self.recorder.start()
        except Exception as e:
            log.exception("mic start failed")
            self._status(f"Microphone error: {e}")
            return
        self.recording = True
        self.press_t = ts
        self._set_icon(ICON_REC)
        play("Tink")
        threading.Thread(target=self.transcriber.prefetch, daemon=True).start()

    def _cancel(self, quiet: bool = False):
        self.recorder.stop()
        self.recording = self.handsfree = False
        self._set_icon(ICON_IDLE)
        if not quiet:
            play("Funk")

    def _finish(self):
        audio = self.recorder.stop()
        log.info("recorded %.1fs", len(audio) / SAMPLE_RATE)
        if len(audio) == 0:
            # The stream opened but macOS delivered no audio: mic access is blocked.
            self.recording = self.handsfree = False
            self._set_icon(ICON_WARN)
            self.missing = missing_permissions(prompt=True)
            log.warning("no audio received; mic status %s",
                        AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio))
            self._status("⚠️ No audio — turn on Local Flow in Microphone settings (menu → Fix permissions)")
            return
        self.recording = self.handsfree = False
        self.busy = True
        self._set_icon(ICON_BUSY)
        # Style comes from where the text will land: the app in front right now.
        try:
            target = current_target(CONFIG["app_styles"])
        except Exception:
            log.exception("couldn't read frontmost app")
            target = Target("", "", "", "default")
        self.jobs.put((audio, target))

    def _worker(self):
        while True:
            audio, target = self.jobs.get()
            self._job_started = time.time()
            self._process(audio, target)
            self._job_started = None
            self.recorder.refresh_devices()  # pick up phones/headsets that came or went
            self._mics_changed = True
            if self.jobs.empty():
                self.busy = False
                if not self.recording:
                    self._set_icon(ICON_IDLE)

    def _process(self, audio: np.ndarray, target: Target):
        seconds = len(audio) / SAMPLE_RATE
        try:
            voiced = voiced_seconds(audio)
            if voiced < 0.25:
                log.info("skipped: no speech detected (voiced %.2fs)", voiced)
                self._status("Didn't catch any speech — is the right mic selected?")
                return
            t0 = time.time()
            raw = self.transcriber.transcribe(audio)
            t1 = time.time()
            if not raw or (raw.strip(" .!?").lower() in HALLUCINATIONS and voiced < 1.0):
                log.info("skipped: empty/hallucinated transcript %r", raw)
                self._status("Didn't catch any speech")
                return
            text = self.cleaner.clean(raw)
            t2 = time.time()
            text, suffix = apply_style(text, target.style)
            if text:
                paste(text + suffix)
                log.info("pasted %d chars into %s [%s] (stt %.2fs)",
                         len(text), target.name or "?", target.style, t1 - t0)
                log_history(raw, text, seconds, t1 - t0, t2 - t1, target)
                self._status(self.cleaner.error or
                             f"Last: {t2 - t0:.1f}s for {seconds:.0f}s of speech")
        except Exception as e:
            log.exception("processing failed")
            self._status(f"Error: {type(e).__name__} (see local-flow.log)")

    # ----- menu -----

    def toggle_local(self, item):
        item.state = not item.state
        self.cleaner.local = CONFIG["local_cleanup"] = bool(item.state)
        save_config()

    def toggle_claude(self, item):
        item.state = not item.state
        self.cleaner.claude = CONFIG["claude_cleanup"] = bool(item.state)
        self.cleaner.error = None
        if item.state and not self.cleaner.claude_available:
            self._status("Claude needs an API key with credits in .env")
        save_config()

    def toggle_sounds(self, item):
        item.state = not item.state
        CONFIG["sounds"] = bool(item.state)
        save_config()

    @rumps.clicked("Edit dictionary")
    def edit_dictionary(self, _):
        subprocess.run(["open", "-t", str(DICTIONARY)])

    @rumps.clicked("Edit snippets")
    def edit_snippets(self, _):
        subprocess.run(["open", "-t", str(SNIPPETS)])

    @rumps.clicked("Search history…")
    def open_history(self, _):
        open_history_page()

    @rumps.clicked("Edit app styles")
    def edit_styles(self, _):
        load_styles()
        subprocess.run(["open", "-t", str(STYLES_PATH)])

    def toggle_overlay(self, item):
        item.state = not item.state
        CONFIG["overlay"] = bool(item.state)
        save_config()

    def toggle_styles(self, item):
        item.state = not item.state
        CONFIG["app_styles"] = bool(item.state)
        save_config()

    def on_wake(self):
        """After sleep: mics may have changed and macOS may have dropped the tap."""
        log.info("woke from sleep — refreshing mics, hotkey and model")
        self.recorder.silent_devices.clear()
        self.recorder.refresh_devices()
        self._mics_changed = True
        self.hotkeys.ensure_healthy()
        self._last_warm = 0  # warm the model on the next tick

    @rumps.clicked("Open log")
    def open_log(self, _):
        subprocess.run(["open", "-t", str(LOG)])

    @rumps.clicked("Restart Local Flow")
    def restart_clicked(self, _):
        restart()

    @rumps.clicked("Fix permissions…")
    def fix_permissions(self, _):
        self.missing = missing_permissions(prompt=True)
        if not self.missing:
            self._status("All permissions OK")
            return
        pane = {"Microphone": "Microphone", "Accessibility": "Accessibility",
                "Input Monitoring": "ListenEvent"}[self.missing[0]]
        subprocess.run(["open", SETTINGS_URL.format(pane)])

    def _build_mic_menu(self):
        current = CONFIG.get("microphone", "auto")
        if self.mic_menu._menu is not None:
            self.mic_menu.clear()
        auto = rumps.MenuItem("Automatic (system default)", callback=self._choose_mic)
        auto.state = current == "auto"
        self.mic_menu.add(auto)
        try:
            devices = input_devices()
        except sd.PortAudioError:  # device list mid re-scan; next refresh fixes it
            devices = []
        for _, name in devices:
            item = rumps.MenuItem(name, callback=self._choose_mic)
            item.state = current == name
            self.mic_menu.add(item)
        self.mic_menu.add(None)
        self.mic_menu.add(rumps.MenuItem("Refresh list", callback=self._refresh_mics))

    def _choose_mic(self, item):
        CONFIG["microphone"] = "auto" if item.title.startswith("Automatic") else item.title
        save_config()
        self._build_mic_menu()
        self._status(f"Mic: {item.title}")

    def _refresh_mics(self, _):
        self.recorder.silent_devices.clear()
        self.recorder.refresh_devices()
        self._mics_changed = True  # rebuilt by _tick once the re-scan settles


def single_instance_lock():
    """Two copies running would paste everything twice — refuse to start a second."""
    handle = open(ROOT / ".lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Local Flow is already running (check the menu bar).")
        sys.exit(0)
    return handle


if __name__ == "__main__":
    _lock = single_instance_lock()
    # Menu-bar only: no Dock icon.
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app = FlowApp()
    rumps.events.on_wake.register(app.on_wake)
    app.run()
