"""Per-app writing styles.

The app (and, for browsers, the window title) under your cursor when you press
the hotkey decides how the text is finished:

  casual    chat apps: a single sentence loses its trailing period ("sounds good")
  formal    email/docs: always starts capitalized and ends with punctuation
  terminal  shells: never pastes a line break (it could run a command), no trailing space
  default   everything else: unchanged

Edit styles.json (menu → Edit app styles) to change which app gets which style.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

from AppKit import NSWorkspace
from ApplicationServices import (AXUIElementCopyAttributeValue, AXUIElementCreateApplication,
                                 kAXFocusedWindowAttribute, kAXTitleAttribute)

STYLES_PATH = Path(__file__).parent / "styles.json"

DEFAULT_STYLES = {
    "apps": {
        "com.apple.MobileSMS": "casual",
        "com.tinyspeck.slackmacgap": "casual",
        "ru.keepcoder.Telegram": "casual",
        "com.hnc.Discord": "casual",
        "net.whatsapp.WhatsApp": "casual",
        "com.apple.mail": "formal",
        "com.microsoft.Outlook": "formal",
        "com.microsoft.Word": "formal",
        "com.apple.iWork.Pages": "formal",
        "com.apple.Terminal": "terminal",
        "com.googlecode.iterm2": "terminal",
        "dev.warp.Warp-Stable": "terminal",
        "com.mitchellh.ghostty": "terminal",
    },
    "window_titles": {
        "Gmail": "formal",
        "Outlook": "formal",
        "Google Docs": "formal",
        "WhatsApp": "casual",
        "Messenger": "casual",
        "Slack": "casual",
        "Discord": "casual",
    },
}


@dataclass
class Target:
    bundle_id: str
    name: str
    title: str
    style: str


def load_styles() -> dict:
    if not STYLES_PATH.exists():
        STYLES_PATH.write_text(json.dumps(DEFAULT_STYLES, indent=2) + "\n")
    try:
        return {**DEFAULT_STYLES, **json.loads(STYLES_PATH.read_text())}
    except json.JSONDecodeError:
        return DEFAULT_STYLES


def _window_title(pid: int) -> str:
    """Focused window title via Accessibility (no Screen Recording permission needed)."""
    app = AXUIElementCreateApplication(pid)
    err, window = AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute, None)
    if err or window is None:
        return ""
    err, title = AXUIElementCopyAttributeValue(window, kAXTitleAttribute, None)
    return "" if err or title is None else str(title)


def current_target(styles_on: bool = True) -> Target:
    """Which app (and style) the dictation will be pasted into."""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return Target("", "", "", "default")
    bundle_id = str(app.bundleIdentifier() or "")
    name = str(app.localizedName() or "")
    try:
        title = _window_title(app.processIdentifier())
    except Exception:
        title = ""
    cfg = load_styles()
    style = "default"
    if styles_on:
        style = cfg["apps"].get(bundle_id, "default")
        if style == "default":
            for needle, s in cfg["window_titles"].items():
                if needle.lower() in title.lower():
                    style = s
                    break
    return Target(bundle_id, name, title, style)


def _single_sentence(text: str) -> bool:
    return "\n" not in text and not re.search(r"[.!?]\s+\S", text)


def apply_style(text: str, style: str) -> tuple[str, str]:
    """Returns (styled text, suffix to paste after it)."""
    text = text.strip()
    if not text:
        return text, ""
    if style == "casual":
        if _single_sentence(text) and text.endswith(".") and not text.endswith(".."):
            text = text[:-1]
        return text, " "
    if style == "formal":
        text = text[0].upper() + text[1:]
        if text[-1].isalnum():
            text += "."
        return text, " "
    if style == "terminal":
        # A pasted newline could execute a half-dictated command.
        text = re.sub(r"\s*\n+\s*", " ", text)
        text = re.sub(r"^- ", "", text)
        if _single_sentence(text) and text.endswith("."):
            text = text[:-1]
        return text, ""
    return text, "" if text.endswith("\n") else " "
