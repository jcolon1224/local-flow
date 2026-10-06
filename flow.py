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
import logging.handlers
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
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSPasteboard,
                    NSPasteboardItem, NSPasteboardTypeString, NSRunningApplication, NSWorkspace)
from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
from Foundation import (NSActivityLatencyCritical, NSActivityUserInitiatedAllowingIdleSystemSleep,
                        NSProcessInfo)
import Quartz

from audio import Recorder, input_devices
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
CONFIG_ERROR = None  # shown in the menu if config.json couldn't be used


def _load_config() -> dict:
    """config.json merged over DEFAULTS, tolerating a damaged or hand-edited file:
    a bad file or a wrong-typed value falls back to the default for that key
    instead of killing the app before the menu exists."""
    global CONFIG_ERROR
    cfg = dict(DEFAULTS)
    try:
        user = json.loads(CONFIG_PATH.read_text())
        if not isinstance(user, dict):
            raise ValueError("config.json must contain an object")
    except (OSError, ValueError) as e:
        CONFIG_ERROR = f"config.json ignored ({type(e).__name__}) — using defaults"
        return cfg
    for key, value in user.items():
        default = DEFAULTS.get(key)
        if default is None:
            cfg[key] = value
        elif isinstance(default, bool):
            cfg[key] = bool(value)
        elif isinstance(default, int):
            try:
                cfg[key] = int(value)
            except (TypeError, ValueError):
                CONFIG_ERROR = f"config.json: {key} must be a number — using {default}"
        else:
            cfg[key] = str(value)
    return cfg


CONFIG = _load_config()

SAMPLE_RATE = 16000  # what Whisper expects
TAP_SECONDS = 0.3  # a press shorter than this is a "tap", not push-to-talk
DOUBLE_TAP_WINDOW = 0.45
KEEP_WARM_SECONDS = 90  # touch the model periodically so macOS doesn't page it out
WATCHDOG_SECONDS = 3
# Whisper invents these on near-silent audio.
HALLUCINATIONS = {"thank you", "thanks for watching", "thank you for watching",
                  "you", "bye", "thank you so much", "subtitles by the amara.org community"}

ICON_IDLE, ICON_REC, ICON_HANDSFREE, ICON_BUSY, ICON_WARN = "🎙", "🔴", "🟠", "⏳", "⚠️"

_log_handler = logging.handlers.RotatingFileHandler(LOG, maxBytes=2_000_000, backupCount=1)
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
log = logging.getLogger("local-flow")


def save_config():
    # Write-then-rename so a crash mid-write can never leave a torn config.json.
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(CONFIG, indent=2) + "\n")
    os.replace(tmp, CONFIG_PATH)


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
# See audio.py: AVAudioEngine capture with silent-mic fallback.


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
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is not None and front.bundleIdentifier():
        # rumps activates us on launch; the new process hands focus straight back.
        os.environ["LOCALFLOW_REACTIVATE"] = str(front.bundleIdentifier())
    os.execv(sys.executable, [sys.executable, str(ROOT / "flow.py")])


# ---------- output ----------

# Clipboard types worth putting back after a paste. Images and file promises
# are skipped: snapshotting them is slow and heavy, and re-setting some of them
# raises inside AppKit.
RESTORABLE_TYPES = ("public.utf8-plain-text", "public.rtf", "public.html", "public.url",
                    "public.file-url", "org.chromium.source-url", "com.apple.webarchive")
RESTORE_MAX_BYTES = 2_000_000
PASTE_GRACE = 1.0  # seconds the target app gets to read the clipboard before we restore it


def _snapshot(pb) -> list[dict]:
    items = []
    for item in pb.pasteboardItems() or []:
        data = {}
        for t in item.types():
            if str(t) in RESTORABLE_TYPES:
                d = item.dataForType_(t)
                if d is not None and d.length() <= RESTORE_MAX_BYTES:
                    data[t] = d
        if data:
            items.append(data)
    return items


def _restore(pb, items: list[dict]):
    try:
        pb.clearContents()
        objs = []
        for data in items:
            obj = NSPasteboardItem.alloc().init()
            for t, d in data.items():
                obj.setData_forType_(d, t)
            objs.append(obj)
        if objs:
            pb.writeObjects_(objs)
    except Exception:
        log.exception("restoring clipboard")


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
        time.sleep(PASTE_GRACE)
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

        self.recorder = Recorder(preferred=lambda: CONFIG.get("microphone", "auto"))
        threading.Thread(target=self.recorder.prewarm, daemon=True, name="audio-prewarm").start()
        self.transcriber = None
        self.lock = threading.Lock()
        self.recording = False
        self.handsfree = False
        self.busy = False  # True while any dictation is waiting/transcribing
        self.jobs: queue.Queue = queue.Queue()  # processed in order by one worker
        self._job: tuple[float, float] | None = None  # (started at, audio seconds)
        self._reactivate_front_app()
        threading.Thread(target=self._worker, daemon=True, name="dictation-worker").start()
        self.press_t = 0.0
        self.last_tap_t = 0.0
        self.hotkey_down = False

        self._icon, self._status_text = ICON_BUSY, None
        self.missing = missing_permissions(prompt=True)
        log.info("missing permissions at start: %s (mic status %s)", self.missing or "none",
                 AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio))
        self._last_perm_check = time.time()
        self._want_prompt = False
        self._restart_when_idle = False
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
            transcriber = Transcriber(CONFIG["model"], CONFIG["language"])
            try:
                transcriber.warm_up()
            except Exception:
                # Model not downloaded yet (first run / changed in config): fetch it.
                log.info("model not cached — downloading")
                self._status("Downloading the speech model (one time, ~500 MB)…")
                import huggingface_hub.constants
                huggingface_hub.constants.HF_HUB_OFFLINE = False
                transcriber.warm_up()
            self.transcriber = transcriber  # only once it's proven to work
            self._set_icon(ICON_IDLE)
            self._show_ready()
            log.info("model ready: %s", CONFIG["model"])
        except Exception as e:
            log.exception("model load failed")
            self._status(f"Model failed to load: {type(e).__name__}")

    def _reactivate_front_app(self):
        bundle = os.environ.pop("LOCALFLOW_REACTIVATE", "")
        if not bundle:
            return

        def give_back():
            time.sleep(0.3)  # after rumps has activated us
            for app in NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle):
                app.activateWithOptions_(0)
                break

        threading.Thread(target=give_back, daemon=True).start()

    def _show_ready(self):
        if self.missing:
            self._status("⚠️ Turn on Local Flow in: " + " & ".join(self.missing))
            self._set_icon(ICON_WARN)
        else:
            self._status(CONFIG_ERROR or f"Ready — hold {self._hotkey_name()} to dictate")

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
        job = self._job  # (started, audio seconds) or None; read once (worker clears it)
        if self.hotkeys.stuck_seconds() > 15:
            reason = "hotkey handler stuck"
        elif job and now - job[0] > 60 + 3 * job[1]:
            reason = f"transcription stuck ({job[1]:.0f}s of audio)"
        elif self.recorder.wedged and not self.recording and not self.busy:
            reason = "audio system hung"
        if reason:
            log.error("self-heal: %s — restarting", reason)
            restart()

    def _idle(self) -> bool:
        return not self.recording and not self.busy

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
        if self.missing and time.time() - self._last_perm_check > 2 and self._idle():
            self._last_perm_check = time.time()
            if not missing_permissions():
                restart()
        if self._want_prompt:  # permission dialogs must come from the main thread
            self._want_prompt = False
            self.missing = missing_permissions(prompt=True)
            self._show_ready()
        # Menus must be rebuilt on the main thread, and not mid re-scan.
        if self._mics_changed and self.recorder.settled():
            self._mics_changed = False
            self._build_mic_menu()
        now = time.time()
        if self._restart_when_idle and self._idle():
            restart()
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
            self.hotkeys.events.put(("press", "cap", now))  # handled on the hotkey thread

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
            if key == "cap":
                if self.recording:
                    log.info("recording hit the %s-minute cap", CONFIG["max_minutes"])
                    self._finish()
            elif self.recording and not self.handsfree:
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
        threading.Thread(target=self._tink_if_still_recording, args=(ts,), daemon=True).start()
        threading.Thread(target=self.transcriber.prefetch, daemon=True).start()

    def _tink_if_still_recording(self, ts: float):
        """Play the start sound only once this is clearly a hold, so Option+arrow
        chords and quick taps stay silent."""
        time.sleep(TAP_SECONDS)
        if self.recording and self.press_t == ts:
            play("Tink")

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
            self.recording = self.handsfree = False
            mic = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
            if mic != 3:  # not authorized: the engine ran but macOS fed it nothing
                log.warning("no audio received; mic status %s", mic)
                self._set_icon(ICON_WARN)
                self._want_prompt = True  # _tick shows the macOS prompt from the main thread
                self._status("⚠️ No audio — turn on Local Flow in Microphone settings (menu → Fix permissions)")
            else:  # e.g. a Bluetooth mic that takes a moment to start delivering
                log.info("no audio captured (too short for this mic)")
                self._set_icon(ICON_IDLE)
                self._status("Nothing recorded — hold the key a little longer")
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
            self._job = (time.time(), len(audio) / SAMPLE_RATE)
            try:
                self._process(audio, target)
            except Exception:  # _process catches its own; this keeps the worker alive regardless
                log.exception("worker")
            self._job = None
            self._mics_changed = True  # phones/headsets may have come or gone
            with self.lock:  # paired with _finish, so a job queued meanwhile isn't marked idle
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
        self.recorder.refresh_devices()
        self._mics_changed = True
        self.hotkeys.ensure_healthy()
        self._last_warm = 0  # warm the model on the next tick

    @rumps.clicked("Open log")
    def open_log(self, _):
        subprocess.run(["open", "-t", str(LOG)])

    @rumps.clicked("Restart Local Flow")
    def restart_clicked(self, _):
        if self._idle():
            restart()
        else:
            self._status("Finishing the current dictation, then restarting…")
            self._restart_when_idle = True

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
            names = [d.name for d in input_devices()]
        except Exception:
            log.exception("listing microphones")
            names = []
        for name in names:
            item = rumps.MenuItem(name, callback=self._choose_mic)
            item.state = current == name
            self.mic_menu.add(item)
        self.mic_menu.add(None)
        self.mic_menu.add(rumps.MenuItem("Refresh list", callback=self._refresh_mics))

    def _choose_mic(self, item):
        CONFIG["microphone"] = "auto" if item.title.startswith("Automatic") else item.title
        save_config()
        self.recorder.refresh_devices()
        self._build_mic_menu()
        self._status(f"Mic: {item.title}")

    def _refresh_mics(self, _):
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
