"""Floating recording pill: a small HUD near the bottom of the screen that shows
live mic levels while you dictate, like Wispr Flow's.

It never takes focus or mouse clicks, so it can't interfere with the app you're
typing into. All methods must be called on the main thread.
"""
from collections import deque

import objc
from AppKit import (NSBackingStoreBuffered, NSBezierPath, NSColor, NSEvent, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName, NSPanel,
                    NSScreen, NSStatusWindowLevel, NSView,
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorIgnoresCycle,
                    NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
                    NSWindowStyleMaskNonactivatingPanel)
from Foundation import NSMakePoint, NSMakeRect, NSPointInRect, NSString

WIDTH, HEIGHT = 148, 36
BARS = 16
BOTTOM_MARGIN = 72

COLORS = {  # mode: (r, g, b)
    "rec": (1.0, 0.27, 0.23),
    "handsfree": (1.0, 0.62, 0.04),
    "busy": (0.75, 0.75, 0.78),
}


class PillView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(PillView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.levels = deque([0.0] * BARS, maxlen=BARS)
        self.mode = "rec"
        self.phase = 0
        return self

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        bounds = self.bounds()
        NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.9).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, HEIGHT / 2, HEIGHT / 2).fill()

        r, g, b = COLORS[self.mode]
        NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0).setFill()
        if self.mode == "busy":
            # Three pulsing dots while transcribing.
            for i in range(3):
                on = (self.phase // 4) % 3 == i
                d = 7 if on else 5
                x = WIDTH / 2 - 16 + i * 16 - d / 2
                NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(x, HEIGHT / 2 - d / 2, d, d)).fill()
            return

        # Mirrored level bars, centered vertically.
        left = 34 if self.mode == "handsfree" else 22
        span = WIDTH - left - 18
        step = span / BARS
        for i, level in enumerate(self.levels):
            h = max(3.0, min(HEIGHT - 14, 3 + level * (HEIGHT - 14)))
            x = left + i * step
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, (HEIGHT - h) / 2, step * 0.55, h), 1.5, 1.5).fill()
        if self.mode == "handsfree":
            # Small "lock" glyph so hands-free is distinguishable at a glance.
            attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(12),
                     NSForegroundColorAttributeName: NSColor.whiteColor()}
            NSString.stringWithString_("🔒").drawAtPoint_withAttributes_(
                NSMakePoint(12, HEIGHT / 2 - 8), attrs)


class RecordingPill:
    def __init__(self):
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        self.panel.setLevel_(NSStatusWindowLevel)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setIgnoresMouseEvents_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary | NSWindowCollectionBehaviorIgnoresCycle)
        self.view = PillView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        self.panel.setContentView_(self.view)
        self.visible = False

    def _place(self):
        """Bottom-center of whichever screen the mouse is on."""
        mouse = NSEvent.mouseLocation()
        screen = next((s for s in NSScreen.screens() if NSPointInRect(mouse, s.frame())),
                      NSScreen.mainScreen())
        if screen is None:  # lid closed / display asleep: nowhere to draw
            return False
        vf = screen.visibleFrame()
        self.panel.setFrameOrigin_(NSMakePoint(
            vf.origin.x + (vf.size.width - WIDTH) / 2, vf.origin.y + BOTTOM_MARGIN))

    def show(self, mode: str):
        if mode != self.view.mode:
            self.view.mode = mode
            self.view.setNeedsDisplay_(True)
        if not self.visible:
            self.view.levels.extend([0.0] * BARS)
            if self._place() is False:
                return
            self.panel.orderFrontRegardless()
            self.visible = True

    def hide(self):
        if self.visible:
            self.panel.orderOut_(None)
            self.visible = False

    def update(self, level: float):
        """Push the current mic level (0..1) and redraw; call ~20x a second."""
        if not self.visible:
            return
        self.view.levels.append(level)
        self.view.phase += 1
        self.view.setNeedsDisplay_(True)
