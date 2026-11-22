"""Native, top-anchored glass shell for Luna's conversation UI."""

import math
import time

import AppKit as AK
from Foundation import NSMakeRect, NSNotificationCenter, NSObject
import objc
import Quartz as Q

from src.ui.chat import (
    VIOLET, ChatContent, HistoryContent, PointerButton, icon_button, label, release_cursor, rich, symbol, white,
)
from src.ui.drop import file_paths, has_files
from src.ui.glass import PAD, DiamondRing, Surface, Wave
from src.ui.motion import Spring


MARGIN_X, MARGIN_BOTTOM = 48, 72  # Room around the island for the corona glow; clicks there pass through.
TOOLBAR = 40
COMPOSE_WIDTH = 440
DROP_HEIGHT = 140
CHAT_RADIUS = 12  # Top flare of the chat island; its visible edge starts this far in.
THINK_PERIOD = 1.4


def notch_path(canvas_width, canvas_height, width, height, top_radius, bottom_radius):
    left, right = (canvas_width - width) / 2, (canvas_width + width) / 2
    top, bottom = canvas_height, canvas_height - height
    t, b, k = top_radius, bottom_radius, 0.55228475
    path = Q.CGPathCreateMutable()
    Q.CGPathMoveToPoint(path, None, left, top)
    Q.CGPathAddCurveToPoint(path, None, left + k*t, top, left + t, top - (1-k)*t,
                          left + t, top - t)
    Q.CGPathAddLineToPoint(path, None, left + t, bottom + b)
    Q.CGPathAddCurveToPoint(path, None, left + t, bottom + (1-k)*b,
                          left + t + (1-k)*b, bottom, left + t + b, bottom)
    Q.CGPathAddLineToPoint(path, None, right - t - b, bottom)
    Q.CGPathAddCurveToPoint(path, None, right - t - (1-k)*b, bottom,
                          right - t, bottom + (1-k)*b, right - t, bottom + b)
    Q.CGPathAddLineToPoint(path, None, right - t, top - t)
    Q.CGPathAddCurveToPoint(path, None, right - t, top - (1-k)*t,
                          right - k*t, top, right, top)
    Q.CGPathCloseSubpath(path)
    return path


class NotchPanel(AK.NSPanel):
    def canBecomeKeyWindow(self):
        return getattr(self, "interactive", False)

    def canBecomeMainWindow(self):
        return False

    def accessibilityPerformRaise(self):
        self.orderFrontRegardless()
        return True


class NotchSurface(AK.NSView):
    def hitTest_(self, point):
        if not Q.CGPathContainsPoint(self.owner.shape.path(), None, point, False):
            return None
        return objc.super(NotchSurface, self).hitTest_(point)

    def draggingEntered_(self, sender):
        point = sender.draggingLocation()
        bounds = Q.CGPathGetPathBoundingBox(self.owner.shape.path())
        near = (bounds.origin.x - 36 <= point[0] <= bounds.origin.x + bounds.size.width + 36
                and bounds.origin.y - 36 <= point[1] <= self.owner.canvas_height)
        if near and has_files(sender.draggingPasteboard()):
            self.owner.enter_drop()
            return AK.NSDragOperationCopy
        self.owner.leave_drop()
        return AK.NSDragOperationNone

    def draggingUpdated_(self, sender):
        return self.draggingEntered_(sender)

    def draggingExited_(self, sender):
        self.owner.leave_drop()

    def prepareForDragOperation_(self, sender):
        return bool(file_paths(sender.draggingPasteboard()))

    def performDragOperation_(self, sender):
        paths = file_paths(sender.draggingPasteboard())
        accepted = self.owner.chat.attach_paths(paths) if paths else False
        self.owner.finish_drop()
        return accepted

    def wantsPeriodicDraggingUpdates(self):
        return True


class NotchWindow(NSObject):
    @objc.python_method
    def build(self, store=None, submit=None):
        self.expanded = False
        self.hovered = self.pinned = self.suppress_hover = False
        self.entered_at = self.left_at = None
        self.mode = "closed"
        self.history_open = self.full_chat = self.drop_hover = False
        self.file_dragging = False
        self.drop_leave_at = None
        self.drag_pasteboard = AK.NSPasteboard.pasteboardWithName_(AK.NSPasteboardNameDrag)
        self.drag_count = self.drag_pasteboard.changeCount()
        self.motion = Spring((0,) * 11)
        self.motion_time = time.monotonic()
        self.speaking = False
        self.audio = {"user": ((0,) * 5, 0), "luna": ((0,) * 5, 0)}
        self.levels = (0,) * 5
        self.last_update = time.monotonic()
        self.glow = 0.0
        self.panel = NotchPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 520, 440),
            AK.NSWindowStyleMaskBorderless | AK.NSWindowStyleMaskNonactivatingPanel,
            AK.NSBackingStoreBuffered, False,
        )
        self.panel.interactive = False
        self.panel.setBecomesKeyOnlyIfNeeded_(True)
        self.panel.setTitle_("Luna")
        self.panel.setAccessibilityRole_(AK.NSAccessibilityWindowRole)
        self.panel.setAccessibilitySubrole_(AK.NSAccessibilityFloatingWindowSubrole)
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(AK.NSColor.clearColor())
        self.panel.setHasShadow_(False)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setLevel_(AK.NSMainMenuWindowLevel + 3)
        self.panel.setCollectionBehavior_(
            AK.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AK.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AK.NSWindowCollectionBehaviorStationary
            | AK.NSWindowCollectionBehaviorIgnoresCycle
        )
        self.panel.setIgnoresMouseEvents_(True)
        surface = NotchSurface.alloc().initWithFrame_(self.panel.contentView().bounds())
        surface.owner = self
        surface.setWantsLayer_(True)
        surface.registerForDraggedTypes_([AK.NSPasteboardTypeFileURL])
        self.panel.setContentView_(surface)
        self.surface = Surface(surface)
        # The shape holds the island path for hit testing; the glass layers fill it.
        self.shape = Q.CAShapeLayer.layer()
        self.shape.setFillColor_(AK.NSColor.clearColor().CGColor())
        self.surface.chrome.layer().addSublayer_(self.shape)
        self.foreground = AK.NSView.alloc().initWithFrame_(surface.bounds())
        self.foreground.setWantsLayer_(True)
        surface.addSubview_(self.foreground)
        self.details = self.foreground.layer()
        self.clip = Q.CAShapeLayer.layer()
        self.details.setMask_(self.clip)
        self.mark = DiamondRing(self.details, 2)
        self.wave = Wave(self.details, 2)
        controls = dict(size=26, radius=8, color=white(0.64), point=13)
        self.history_button = icon_button("clock.arrow.circlepath", "Chat history", self, "showHistory:", **controls)
        self.title_button = PointerButton.alloc().initWithFrame_(NSMakeRect(0, 0, 200, 28))
        self.title_button.setBordered_(False)
        self.title_button.cell().setLineBreakMode_(AK.NSLineBreakByTruncatingTail)
        self.title_button.setTarget_(self)
        self.title_button.setAction_("showHistory:")
        self.title_button.setWantsLayer_(True)
        self.title_button.layer().setCornerRadius_(8)
        self.title = None
        self.new_button = icon_button("square.and.pencil", "New chat", self, "newChat:", **controls)
        self.pin_button = icon_button("pin", "Pin conversation", self, "togglePin:", **controls)
        for button in (self.history_button, self.title_button, self.new_button, self.pin_button):
            self.foreground.addSubview_(button)
        self.chat = ChatContent.alloc().init()
        self.chat.build(self, store=store, submit=submit)
        self.chat.view.setWantsLayer_(True)
        self.chat.view.layer().setMasksToBounds_(True)
        self.foreground.addSubview_(self.chat.view)
        self.history = HistoryContent.alloc().init()
        self.history.build(self)
        self.history.view.setWantsLayer_(True)
        self.foreground.addSubview_(self.history.view)
        self.drop_view = AK.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 80))
        self.drop_view.setWantsLayer_(True)
        self.drop_edge = Q.CAShapeLayer.layer()
        self.drop_edge.setFillColor_(VIOLET.colorWithAlphaComponent_(0.08).CGColor())
        self.drop_edge.setStrokeColor_(VIOLET.colorWithAlphaComponent_(0.7).CGColor())
        self.drop_edge.setLineWidth_(1)
        self.drop_edge.setLineDashPattern_([5, 4])
        self.drop_view.layer().addSublayer_(self.drop_edge)
        self.drop_icon = AK.NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 24, 24))
        self.drop_icon.setImage_(symbol("arrow.down.to.line", VIOLET.colorWithAlphaComponent_(0.9), 18))
        self.drop_view.addSubview_(self.drop_icon)
        self.drop_label = label("Drop to attach", 13.5, AK.NSColor.whiteColor())
        self.drop_label.setAlignment_(AK.NSTextAlignmentCenter)
        self.drop_view.addSubview_(self.drop_label)
        self.drop_hint = label("Files go with your next message", 12.5, white(0.64))
        self.drop_hint.setAlignment_(AK.NSTextAlignmentCenter)
        self.drop_view.addSubview_(self.drop_hint)
        self.foreground.addSubview_(self.drop_view)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "screenChanged:", AK.NSApplicationDidChangeScreenParametersNotification, None,
        )
        self.layout()

    @objc.python_method
    def layout(self):
        screens = AK.NSScreen.screens()
        if not screens:
            self.panel.orderOut_(None)
            return
        screen = next((s for s in screens if s.safeAreaInsets().top > 0), screens[0])
        frame = screen.frame()
        left, right = screen.auxiliaryTopLeftArea(), screen.auxiliaryTopRightArea()
        self.closed_height = screen.safeAreaInsets().top
        self.closed_width = (
            right.origin.x - left.origin.x - left.size.width + 12
            if self.closed_height and left and right else 0
        )
        self.width = min(2 * math.ceil(((self.closed_width or 197) + 96) / 2), frame.size.width)
        self.height = self.closed_height or 32
        self.chat_width = min(520, frame.size.width - 24)
        self.chat_height = min(480, frame.size.height - 120)
        self.compose_width = min(COMPOSE_WIDTH, self.chat_width)
        self.transcript_width = self.chat_width - 2 * (CHAT_RADIUS + PAD)
        self.canvas_width = self.chat_width + 2 * MARGIN_X
        self.canvas_height = self.chat_height + MARGIN_BOTTOM
        self.panel.setFrame_display_(NSMakeRect(
            frame.origin.x + (frame.size.width - self.canvas_width) / 2,
            frame.origin.y + frame.size.height - self.canvas_height, self.canvas_width, self.canvas_height,
        ), False)
        bounds = self.panel.contentView().bounds()
        scale = screen.backingScaleFactor()
        self.surface.layout(bounds, scale)
        for layer in (self.shape, self.clip):
            layer.setFrame_(bounds)
            layer.setContentsScale_(scale)
        self.foreground.setFrame_(bounds)
        for item in (self.mark.layer, self.mark.ring, self.mark.bead, self.wave.layer):
            item.setContentsScale_(scale)
        self._draw(animate=False)
        self.update_audio()
        self.panel.orderFrontRegardless()

    def screenChanged_(self, notification):
        self.layout()

    @objc.python_method
    def set_expanded(self, expanded):
        if self.expanded != expanded:
            self.expanded = expanded
            self.speaking = False
            self.audio = {"user": ((0,) * 5, 0), "luna": ((0,) * 5, 0)}
            self.levels = (0,) * 5
            self._present()
            self.update_audio()

    @objc.python_method
    def _desired_mode(self):
        if self.hovered or self.pinned or self.drop_hover:
            if self.history_open and not self.drop_hover:
                return "history"
            # Hover always starts compact; only sending or picking a chat opens the full window.
            return "chat" if self.full_chat else "compose"
        return "voice" if self.expanded else "closed"

    @objc.python_method
    def _settle(self):
        """Once the island collapses, the next hover starts again from the compact composer."""
        if not (self.hovered or self.pinned or self.drop_hover):
            self.full_chat = self.history_open = False

    @objc.python_method
    def _present(self):
        if self._desired_mode() != self.mode:
            self._draw(animate=True)

    @objc.python_method
    def update_hover(self, point=None, now=None):
        now = time.monotonic() if now is None else now
        point = AK.NSEvent.mouseLocation() if point is None else point
        frame = self.panel.frame()
        local = (point[0] - frame.origin.x, point[1] - frame.origin.y)
        inside = Q.CGPathContainsPoint(self.shape.path(), None, local, False)
        if self.mode == "closed" and not self.closed_width:
            inside = abs(local[0] - self.canvas_width / 2) < 98 and local[1] > self.canvas_height - self.height
        if inside:
            self.left_at = None
            if not self.hovered and not self.suppress_hover:
                if self.entered_at is None:
                    self.entered_at = now
                elif now - self.entered_at >= 0.18:
                    self.hovered = True
                    self._present()
        else:
            self.entered_at = None
            self.suppress_hover = False
            if self.hovered:
                if self.left_at is None:
                    self.left_at = now
                elif now - self.left_at >= 0.35:
                    self.hovered = False
                    self._settle()
                    self._present()
        interactive = (inside or self.drop_hover) and self.mode in ("compose", "chat", "history")
        if not interactive:
            # An ignoring or collapsing panel gets no mouseExited, so it must hand back any hover cursor itself.
            release_cursor()
        self.panel.setIgnoresMouseEvents_(not (interactive or self.file_dragging))

    @objc.python_method
    def tick(self):
        now = time.monotonic()
        elapsed = min(now - self.motion_time, 0.2)
        self.update_drag(now=now)
        self.update_hover()
        self.chat.tick(now)
        if self.motion.moving:
            self.motion.advance(now - self.motion_time)
            self._render_geometry()
        self.motion_time = now
        if self.mode == "voice":
            self.update_audio()
        self.update_state(now, elapsed)

    @objc.python_method
    def update_state(self, now, elapsed):
        """Drive the mark and the corona from what Luna is doing right now."""
        text = self.mode in ("compose", "chat", "history")
        working = text and self.chat.pending is not None
        voice = self.mode == "voice"
        level = sum(self.levels) / len(self.levels)
        if working:
            state = "thinking"
        elif voice:
            state = "speaking" if self.speaking else "listening"
        elif self.chat.attention:
            state = "attention"
        else:
            state = "ready"
        reduce = AK.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
        phase = 0.0 if reduce else (now % THINK_PERIOD) / THINK_PERIOD
        target = 1.0 if working or (voice and self.speaking) else 0.0
        if reduce:
            self.glow = target
        else:
            self.glow += (target - self.glow) * (1 - math.exp(-elapsed / 0.25))
            if abs(self.glow - target) < 0.002:
                self.glow = target
        Q.CATransaction.begin()
        Q.CATransaction.setDisableActions_(True)
        self.mark.set_state(state, level, phase)
        # Speaking modulates the corona with Luna's voice; work holds it steady.
        self.surface.set_glow(self.glow * (0.7 + 0.3 * level if voice and self.speaking else 1))
        Q.CATransaction.commit()

    @objc.python_method
    def open_chat(self):
        self.hovered = True
        self._present()

    def showHistory_(self, sender):
        if self.history_open:
            self.closeHistory_(sender)
            return
        if not self.chat.flush_draft(force=True):
            return
        self.history_open = True
        self.history.search.setStringValue_("")
        self.history.refresh()
        self.hovered = True
        self._draw(animate=True)
        self.panel.makeKeyWindow()
        self.panel.makeFirstResponder_(self.history.search)
        editor = self.history.search.currentEditor()
        if editor is not None:
            editor.setInsertionPointColor_(VIOLET)

    def closeHistory_(self, sender):
        """Esc in search: back to writing, in whichever size the island had before."""
        self.history_open = False
        self._draw(animate=True)
        if self.panel.isKeyWindow():
            self.panel.makeFirstResponder_(self.chat.prompt)

    def newChat_(self, sender):
        if self.chat.new_chat():
            self.history_open = False
            self.full_chat = False
            self.hovered = True
            self._draw(animate=True)
            self.panel.makeKeyWindow()
            self.panel.makeFirstResponder_(self.chat.prompt)

    @objc.python_method
    def enter_drop(self):
        self.drop_leave_at = None
        if not self.drop_hover:
            self.drop_hover = True
            self.suppress_hover = False
            self._draw(animate=True)
        self.panel.setIgnoresMouseEvents_(False)

    @objc.python_method
    def leave_drop(self):
        if self.drop_leave_at is None:
            self.drop_leave_at = time.monotonic() + 0.45

    @objc.python_method
    def finish_drop(self):
        self.drop_hover = False
        self.history_open = False
        self.hovered = True
        self.left_at = None
        self._draw(animate=True)

    @objc.python_method
    def update_drag(self, *, now=None, point=None, buttons=None, pasteboard=None):
        now = time.monotonic() if now is None else now
        pasteboard = self.drag_pasteboard if pasteboard is None else pasteboard
        buttons = AK.NSEvent.pressedMouseButtons() if buttons is None else buttons
        point = AK.NSEvent.mouseLocation() if point is None else point
        if not buttons & 1:
            self.file_dragging = False
            self.drag_count = pasteboard.changeCount()
            if self.drop_hover:
                self.leave_drop()
        elif pasteboard.changeCount() != self.drag_count and has_files(pasteboard):
            self.file_dragging = True
            self.panel.setIgnoresMouseEvents_(False)
            frame = self.panel.frame()
            bounds = Q.CGPathGetPathBoundingBox(self.shape.path())
            x, y = point[0] - frame.origin.x, point[1] - frame.origin.y
            near = (bounds.origin.x - 36 <= x <= bounds.origin.x + bounds.size.width + 36
                    and bounds.origin.y - 36 <= y <= self.canvas_height)
            if near:
                self.enter_drop()
            elif self.drop_hover:
                self.leave_drop()
        if self.drop_leave_at is not None and now >= self.drop_leave_at:
            self.drop_hover = False
            self.drop_leave_at = None
            self._draw(animate=True)

    def togglePin_(self, sender):
        self.pinned = not self.pinned
        self.hovered = True
        self.pin_button.setImage_(symbol("pin.fill" if self.pinned else "pin",
                                         AK.NSColor.whiteColor() if self.pinned else white(0.64), 13))
        label = "Unpin conversation" if self.pinned else "Pin conversation"
        self.pin_button.setToolTip_(label)
        self.pin_button.setAccessibilityLabel_(label)
        self.pin_button.layer().setBackgroundColor_(white(0.16 if self.pinned else 0).CGColor())
        self.pin_button.setAccessibilityValue_("Pinned" if self.pinned else "Unpinned")
        self._present()

    @objc.python_method
    def dismiss(self):
        if self.pinned:
            self.togglePin_(None)
        self.hovered = False
        self.entered_at = self.left_at = None
        self.suppress_hover = True
        self._settle()
        self._present()

    @objc.python_method
    def set_audio(self, speaker, levels, timestamp):
        if self.expanded:
            self.audio[speaker] = (levels or (0,) * 5, timestamp)
            if speaker == "luna":
                self.speaking = levels is not None

    @objc.python_method
    def update_audio(self):
        now = time.monotonic()
        elapsed, self.last_update = min(now - self.last_update, 0.2), now
        target, timestamp = self.audio["luna" if self.speaking else "user"]
        if now - timestamp > 0.12 or not self.expanded:
            target = (0,) * 5
        self.levels = tuple(
            old + (new - old) * (1 - math.exp(-elapsed / (0.04 if new > old else 0.14)))
            for old, new in zip(self.levels, target)
        )
        reduce = AK.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
        Q.CATransaction.begin()
        Q.CATransaction.setDisableActions_(True)
        self.wave.update(self.levels, self.speaking, 0 if reduce else (now % 1.6) / 1.6)
        Q.CATransaction.commit()

    @objc.python_method
    def _update_title(self):
        title = self.chat.store.current["title"]
        key = (title, self.history_open)
        if key == self.title:
            return
        self.title = key
        # The chevron is a text attachment centred on the cap height, so it always lines up with the title.
        self.title_button.setAttributedTitle_(rich(
            [title + "  ", ("chevron.down", white(0.48))], 13.5, AK.NSColor.whiteColor(), AK.NSFontWeightSemibold))
        self.title_button.setAccessibilityLabel_(f"{title}, show chat history")
        self.title_button.setToolTip_("Show chat history")
        self.history_button.layer().setBackgroundColor_(white(0.16 if self.history_open else 0).CGColor())

    @objc.python_method
    def _draw(self, *, animate):
        if not hasattr(self, "width"):
            return
        self.mode = self._desired_mode()
        text_mode = self.mode in ("compose", "chat", "history")
        compact = self.compose_width
        if self.drop_hover and self.mode != "chat":
            width, height, top, bottom = compact, self.height + DROP_HEIGHT, 10, 26
        elif self.mode == "chat":
            width, height, top, bottom = self.chat_width, self.chat_height, CHAT_RADIUS, 30
        elif self.mode == "history":
            self.history.view.setFrame_(NSMakeRect(0, 0, compact - 2 * (10 + PAD), 300))
            height = min(self.chat_height, self.height + self.history.content_height + PAD)
            width, top, bottom = compact, 10, 26
        elif self.mode == "compose":
            width, height = compact, self.height + 8 + self.chat.composer_height + PAD
            top, bottom = 10, 26
        elif self.mode == "voice":
            width, height, top, bottom = self.width, self.height, 6, 12
        else:
            width, height = self.closed_width, self.closed_height
            top, bottom = (6, 10) if width else (0, 0)
        immediate = not animate or AK.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
        chat = self.mode in ("compose", "chat") and not self.drop_hover
        target = (width, height, top, bottom, float(chat), float(self.mode == "history" and not self.drop_hover),
                  float(self.mode == "voice"), float(self.mode == "chat"),
                  float(self.mode in ("compose", "history") and not self.drop_hover),
                  float(self.drop_hover), float(self.mode == "chat"))
        self.motion.move_to(target, immediate=immediate, closing=self.mode == "closed")
        self.motion_time = time.monotonic()
        self.panel.interactive = text_mode
        if not text_mode and self.panel.isKeyWindow():
            self.panel.makeFirstResponder_(None)
            self.panel.resignKeyWindow()
        self.chat.set_placeholder("Reply to Luna" if self.full_chat else "Ask Luna anything")
        self._update_title()
        self._render_geometry()

    @objc.python_method
    def _render_geometry(self):
        width, height, top, bottom, chat_a, history_a, voice_a, bar_a, wing_a, drop_a, full_a = self.motion.value
        width, height = max(0, width), max(0, height)
        path = notch_path(self.canvas_width, self.canvas_height, width, height, max(0, top), max(0, bottom))
        header_width = max(width, self.width)
        left = (self.canvas_width - header_width) / 2
        header_y = self.canvas_height - self.height / 2
        island_left = (self.canvas_width - width) / 2 + max(0, top)
        island_right = (self.canvas_width + width) / 2 - max(0, top)
        toolbar_y = self.canvas_height - self.height - 6 - 28
        offset = self.height + 4 + TOOLBAR * max(0, min(1, bar_a))
        # The path's top flare sits outside the visible edge, so content insets by flare + PAD on each side.
        body_width = max(1, island_right - island_left - 2 * PAD)
        body_height = max(1, height - offset - PAD)
        body_rect = NSMakeRect(island_left + PAD, self.canvas_height - height + PAD, body_width, body_height)
        reveal = min(1, max(0, (height - self.height - 16) / 40))
        reveal = reveal * reveal * (3 - 2 * reveal)
        bar = max(0, min(1, bar_a))
        Q.CATransaction.begin()
        Q.CATransaction.setDisableActions_(True)
        self.shape.setPath_(path)
        self.clip.setPath_(path)
        self.surface.update(path, self.canvas_height, height, self.height)
        self.mark.layer.setPosition_((left + 30, header_y))
        self.mark.layer.setOpacity_(1 if width > self.closed_width + 20 or not self.closed_width else 0)
        self.wave.layer.setPosition_((left + header_width - 40, header_y))
        self.wave.layer.setOpacity_(max(0, min(1, voice_a)))
        # New chat and pin glide between the notch's right wing (compact) and the chat toolbar.
        wing_right = left + header_width - 12
        wing_y = header_y - 13
        self.history_button.setFrame_(NSMakeRect(wing_right - 3 * 28, wing_y, 26, 26))
        for index, button in ((1, self.new_button), (0, self.pin_button)):
            wing_x = wing_right - (index + 1) * 28
            bar_x = island_right - PAD - (index + 1) * 32
            size = 26 + 2 * bar
            button.setFrame_(NSMakeRect(wing_x + (bar_x - wing_x) * bar, wing_y + (toolbar_y - wing_y) * bar,
                                        size, size))
        title_width = min(self.title_button.fittingSize().width + 20, max(80, island_right - island_left - 112))
        self.title_button.setFrame_(NSMakeRect(island_left + PAD, toolbar_y, title_width, 28))
        compact_controls = max(0, min(1, wing_a))
        controls = ((self.history_button, compact_controls), (self.title_button, bar),
                    (self.new_button, max(compact_controls, bar)), (self.pin_button, max(compact_controls, bar)))
        for view, alpha in controls:
            view.setHidden_(alpha < 0.003)
            view.setAlphaValue_(alpha)
        for view, alpha in ((self.chat.view, chat_a), (self.history.view, history_a), (self.drop_view, drop_a)):
            alpha = max(0, min(1, alpha)) * reveal
            view.setFrame_(body_rect)
            view.setHidden_(alpha < 0.003)
            view.setAlphaValue_(alpha)
        self.chat.layout(body_width, body_height, full_a > 0.5)
        self.history.layout(body_width, body_height)
        edge = Q.CGPathCreateWithRoundedRect(NSMakeRect(0.5, 0.5, body_width - 1, body_height - 1), 14, 14, None)
        self.drop_edge.setFrame_(NSMakeRect(0, 0, body_width, body_height))
        self.drop_edge.setPath_(edge)
        middle = body_height / 2
        self.drop_icon.setFrame_(NSMakeRect((body_width - 24) / 2, middle + 12, 24, 24))
        self.drop_label.setFrame_(NSMakeRect(8, middle - 10, max(1, body_width - 16), 18))
        self.drop_hint.setFrame_(NSMakeRect(8, middle - 30, max(1, body_width - 16), 16))
        Q.CATransaction.commit()

    @objc.python_method
    def close(self):
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        self.shape.removeAllAnimations()
        try:
            self.chat.close()
        finally:
            self.panel.close()
