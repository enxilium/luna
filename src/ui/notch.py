"""Native, top-anchored shell for Luna's conversation UI."""

import math
import time

import AppKit as AK
from Foundation import NSMakeRect, NSNotificationCenter, NSObject, NSValue
import objc
import Quartz as Q

from src.ui.chat import ChatContent, HistoryContent, icon_button, symbol
from src.ui.drop import file_paths, has_files
from src.ui.motion import Spring


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
    def build(self, store=None):
        self.expanded = False
        self.hovered = self.pinned = self.suppress_hover = False
        self.entered_at = self.left_at = None
        self.mode = "closed"
        self.history_open = self.full_chat = self.drop_hover = False
        self.file_dragging = False
        self.drop_leave_at = None
        self.drag_pasteboard = AK.NSPasteboard.pasteboardWithName_(AK.NSPasteboardNameDrag)
        self.drag_count = self.drag_pasteboard.changeCount()
        self.motion = Spring((0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self.motion_time = time.monotonic()
        self.speaking = False
        self.audio = {"user": ((0,) * 5, 0), "luna": ((0,) * 5, 0)}
        self.levels = (0,) * 5
        self.last_update = time.monotonic()
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
        self.shape = Q.CAShapeLayer.layer()
        self.shape.setFillColor_(AK.NSColor.blackColor().CGColor())
        self.panel.contentView().layer().addSublayer_(self.shape)
        self.foreground = AK.NSView.alloc().initWithFrame_(surface.bounds())
        self.foreground.setWantsLayer_(True)
        surface.addSubview_(self.foreground)
        self.details = self.foreground.layer()
        self.clip = Q.CAShapeLayer.layer()
        self.details.setMask_(self.clip)
        self.moon = Q.CALayer.layer()
        self.moon.setContents_(symbol("moon.fill"))
        self.moon.setContentsGravity_(Q.kCAGravityResizeAspect)
        self.details.addSublayer_(self.moon)
        self.visualizer = Q.CAGradientLayer.layer()
        self.visualizer.setStartPoint_((0, 0))
        self.visualizer.setEndPoint_((1, 1))
        self.bars = Q.CAShapeLayer.layer()
        self.visualizer.setMask_(self.bars)
        self.details.addSublayer_(self.visualizer)
        self.history_button = icon_button("clock.arrow.circlepath", "Chat history", self, "showHistory:")
        self.new_button = icon_button("square.and.pencil", "New chat", self, "newChat:")
        self.pin_button = icon_button("pin", "Pin conversation", self, "togglePin:")
        self.foreground.addSubview_(self.history_button)
        self.foreground.addSubview_(self.new_button)
        self.foreground.addSubview_(self.pin_button)
        self.chat = ChatContent.alloc().init()
        self.chat.build(self, store=store)
        self.chat.view.setWantsLayer_(True)
        self.chat.view.layer().setMasksToBounds_(True)
        self.foreground.addSubview_(self.chat.view)
        self.history = HistoryContent.alloc().init()
        self.history.build(self)
        self.history.view.setWantsLayer_(True)
        self.foreground.addSubview_(self.history.view)
        self.drop_view = AK.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 80))
        self.drop_view.setWantsLayer_(True)
        self.drop_view.layer().setCornerRadius_(14)
        self.drop_view.layer().setBackgroundColor_(AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.09, 0.07, 0.16, 1).CGColor())
        self.drop_view.layer().setBorderWidth_(1)
        self.drop_view.layer().setBorderColor_(AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.55, 0.43, 0.9, 1).CGColor())
        self.drop_icon = AK.NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 24, 24))
        self.drop_icon.setImage_(symbol("arrow.down.doc"))
        self.drop_view.addSubview_(self.drop_icon)
        self.drop_label = AK.NSTextField.labelWithString_("Drop files to attach")
        self.drop_label.setAlignment_(AK.NSTextAlignmentCenter)
        self.drop_label.setFont_(AK.NSFont.systemFontOfSize_(12))
        self.drop_label.setTextColor_(AK.NSColor.whiteColor())
        self.drop_view.addSubview_(self.drop_label)
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
        self.chat_height = min(440, frame.size.height - 64)
        self.canvas_width = self.chat_width + 24
        self.canvas_height = self.chat_height + 16
        self.panel.setFrame_display_(NSMakeRect(
            frame.origin.x + (frame.size.width - self.canvas_width) / 2,
            frame.origin.y + frame.size.height - self.canvas_height, self.canvas_width, self.canvas_height,
        ), False)
        self.shape.setFrame_(self.panel.contentView().bounds())
        self.foreground.setFrame_(self.panel.contentView().bounds())
        self.clip.setFrame_(self.panel.contentView().bounds())
        self.moon.setBounds_(NSMakeRect(0, 0, 16, 16))
        self.moon.setContentsScale_(screen.backingScaleFactor())
        self.visualizer.setBounds_(NSMakeRect(0, 0, 23, 16))
        self.bars.setFrame_(self.visualizer.bounds())
        self.shape.setContentsScale_(screen.backingScaleFactor())
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
            return "chat" if self.chat.messages or self.full_chat else "compose"
        return "voice" if self.expanded else "closed"

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
                    self._present()
        interactive = (inside or self.drop_hover) and self.mode in ("compose", "chat", "history")
        self.panel.setIgnoresMouseEvents_(not (interactive or self.file_dragging))

    @objc.python_method
    def tick(self):
        now = time.monotonic()
        self.update_drag(now=now)
        self.update_hover()
        self.chat.tick(now)
        if self.motion.moving:
            self.motion.advance(now - self.motion_time)
            self._render_geometry()
        self.motion_time = now
        if self.mode == "voice":
            self.update_audio()

    @objc.python_method
    def open_chat(self):
        self.hovered = True
        self._present()

    def showHistory_(self, sender):
        if not self.chat.flush_draft(force=True):
            return
        self.history_open = not self.history_open
        if self.history_open:
            self.history.refresh()
        self.hovered = True
        self._present()

    def newChat_(self, sender):
        if self.chat.new_chat():
            self.history_open = False
            self.full_chat = True
            self.hovered = True
            self._present()
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
        self.pin_button.setImage_(symbol("pin.fill" if self.pinned else "pin"))
        label = "Unpin conversation" if self.pinned else "Pin conversation"
        self.pin_button.setToolTip_(label)
        self.pin_button.setAccessibilityLabel_(label)
        self.pin_button.layer().setBackgroundColor_(
            AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.25, 0.18, 0.38, 1 if self.pinned else 0).CGColor(),
        )
        self.pin_button.setAccessibilityValue_("Pinned" if self.pinned else "Unpinned")
        self._present()

    @objc.python_method
    def dismiss(self):
        if self.pinned:
            self.togglePin_(None)
        self.hovered = False
        self.entered_at = self.left_at = None
        self.suppress_hover = True
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
        path = Q.CGPathCreateMutable()
        for i, level in enumerate(self.levels):
            height = 2 + 14 * level
            Q.CGPathAddRoundedRect(path, None, NSMakeRect(i * 5, (16 - height) / 2, 3, height), 1.5, 1.5)
        colors = (
            [AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.35, 0.55, 1, 1).CGColor(),
             AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.75, 0.4, 1, 1).CGColor()]
            if self.speaking else [AK.NSColor.whiteColor().CGColor()] * 2
        )
        Q.CATransaction.begin()
        Q.CATransaction.setDisableActions_(True)
        self.bars.setPath_(path)
        self.visualizer.setColors_(colors)
        Q.CATransaction.commit()

    @objc.python_method
    def _draw(self, *, animate):
        if not hasattr(self, "width"):
            return
        self.mode = self._desired_mode()
        text_mode = self.mode in ("compose", "chat", "history")
        if self.mode in ("chat", "history"):
            width, height, top, bottom = self.chat_width, self.chat_height, 10, 24
        elif self.mode == "compose":
            width, height, top, bottom = self.width, self.height + self.chat.composer_height + 24, 6, 18
        elif self.mode == "voice":
            width, height, top, bottom = self.width, self.height, 6, 10
        else:
            width, height = self.closed_width, self.closed_height
            top, bottom = (6, 10) if width else (0, 0)
        immediate = not animate or AK.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
        target = (width, height, top, bottom,
                  float(self.mode in ("compose", "chat") and not self.drop_hover),
                  float(self.mode == "history"), float(self.mode == "voice"),
                  float(self.mode in ("chat", "history")), float(self.mode in ("chat", "history")),
                  float(self.drop_hover), self.chat.composer_height)
        self.motion.move_to(target, immediate=immediate, closing=self.mode == "closed")
        self.motion_time = time.monotonic()
        self.panel.interactive = text_mode
        if not text_mode and self.panel.isKeyWindow():
            self.panel.makeFirstResponder_(None)
            self.panel.resignKeyWindow()
        self._render_geometry()

    @objc.python_method
    def _render_geometry(self):
        width, height, top, bottom, body_a, history_a, voice_a, pin_a, new_a, drop_a, composer_h = self.motion.value
        width, height = max(0, width), max(0, height)
        path = notch_path(self.canvas_width, self.canvas_height, width, height, max(0, top), max(0, bottom))
        header_width = max(width, self.width)
        left = (self.canvas_width - header_width) / 2
        header_y = self.canvas_height - self.height / 2
        body_width = max(1, width - 32)
        body_height = max(56, height - self.height - 24)
        body_rect = NSMakeRect((self.canvas_width - width) / 2 + 16,
                               self.canvas_height - height + 12, body_width, body_height)
        reveal = min(1, max(0, (height - self.height - 24) / max(56, composer_h)))
        reveal = reveal * reveal * (3 - 2 * reveal)
        Q.CATransaction.begin()
        Q.CATransaction.setDisableActions_(True)
        self.shape.setPath_(path)
        self.clip.setPath_(path)
        self.shape.setStrokeColor_(AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.59, 0.43, 0.96, 1).CGColor())
        self.shape.setLineWidth_(max(0, min(1, drop_a)) * 1.25)
        self.moon.setPosition_((left + 30, header_y))
        self.visualizer.setPosition_((left + header_width - 30, header_y))
        self.visualizer.setOpacity_(max(0, min(1, voice_a)))
        self.history_button.setFrame_(NSMakeRect(left + header_width - 44, header_y - 14, 28, 28))
        self.new_button.setFrame_(NSMakeRect(left + header_width - 76, header_y - 14, 28, 28))
        self.pin_button.setFrame_(NSMakeRect(left + header_width - 108, header_y - 14, 28, 28))
        text_a = max(body_a, history_a, drop_a)
        for view, alpha in ((self.history_button, text_a), (self.new_button, new_a), (self.pin_button, pin_a)):
            alpha = max(0, min(1, alpha))
            view.setHidden_(alpha < 0.003)
            view.setAlphaValue_(alpha)
        for view, alpha in ((self.chat.view, body_a), (self.history.view, history_a), (self.drop_view, drop_a)):
            alpha = max(0, min(1, alpha)) * reveal
            view.setFrame_(body_rect)
            view.setHidden_(alpha < 0.003)
            view.setAlphaValue_(alpha)
        self.chat.layout(body_width, body_height, self.mode == "chat" or body_height > 100,
                         composer_height=max(56, composer_h))
        self.history.layout(self.chat_width - 32, max(56, self.chat_height - self.height - 24))
        self.drop_icon.setFrame_(NSMakeRect((body_width - 24) / 2, (body_height - 24) / 2 + 10, 24, 24))
        self.drop_label.setFrame_(NSMakeRect(8, max(2, body_height / 2 - 24), max(1, body_width - 16), 20))
        Q.CATransaction.commit()

    @objc.python_method
    def close(self):
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        self.shape.removeAllAnimations()
        try:
            self.chat.close()
        finally:
            self.panel.close()
