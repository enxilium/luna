"""Native chat controls, local history, and a simulated reply stream."""

import math
import os
from pathlib import Path
import sqlite3
import time

import AppKit as AK
from Foundation import NSAttributedString, NSMakeRect, NSMakeSize, NSObject
import objc
import Quartz as Q

from src.ui.chats import ChatStore
from src.ui.motion import Spring


BLUE = AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.23, 0.35, 0.77, 1)
PURPLE = AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.46, 0.28, 0.72, 1)


def symbol(name):
    image = AK.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    return image.imageWithSymbolConfiguration_(
        AK.NSImageSymbolConfiguration.configurationWithPaletteColors_([AK.NSColor.whiteColor()]),
    )


def icon_button(name, label, target, action):
    button = AK.NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 28, 28))
    button.setTitle_("")
    button.setBordered_(False)
    button.setImage_(symbol(name))
    button.setImageScaling_(AK.NSImageScaleProportionallyDown)
    button.setToolTip_(label)
    button.setAccessibilityLabel_(label)
    button.setTarget_(target)
    button.setAction_(action)
    button.setWantsLayer_(True)
    button.layer().setCornerRadius_(9)
    return button


class ChatDocument(AK.NSView):
    def isFlipped(self):
        return True


class PlaceholderField(AK.NSTextField):
    def hitTest_(self, point):
        return None


class PromptView(AK.NSTextView):
    def needsPanelToBecomeKey(self):
        return True

    def insertNewline_(self, sender):
        event = AK.NSApplication.sharedApplication().currentEvent()
        if self.hasMarkedText() or (event and event.modifierFlags() & AK.NSEventModifierFlagShift):
            objc.super(PromptView, self).insertNewline_(sender)
        else:
            self.owner.send_(sender)

    def cancelOperation_(self, sender):
        self.owner.owner.dismiss()

    def performKeyEquivalent_(self, event):
        if event.modifierFlags() & AK.NSEventModifierFlagCommand:
            key = event.charactersIgnoringModifiers().lower()
            action = {"a": "selectAll_", "c": "copy_", "x": "cut_", "v": "paste_"}.get(key)
            if action:
                getattr(self, action)(None)
                return True
            if key == "z" and self.undoManager():
                if event.modifierFlags() & AK.NSEventModifierFlagShift:
                    self.undoManager().redo()
                else:
                    self.undoManager().undo()
                return True
        return objc.super(PromptView, self).performKeyEquivalent_(event)


class Bubble(AK.NSView):
    @objc.python_method
    def build(self, role, text, files=()):
        self.role, self.text = role, text
        self.files = tuple(files)
        self.file_rows = []
        self.motion = None
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(14)
        self.layer().setMasksToBounds_(True)
        if role == "luna":
            self.gradient = Q.CAGradientLayer.layer()
            self.gradient.setColors_([BLUE.CGColor(), PURPLE.CGColor()])
            self.gradient.setStartPoint_((0, 1))
            self.gradient.setEndPoint_((1, 0))
            self.layer().addSublayer_(self.gradient)
        else:
            self.gradient = None
            self.layer().setBackgroundColor_(AK.NSColor.colorWithWhite_alpha_(0.15, 1).CGColor())
        self.label = AK.NSTextField.wrappingLabelWithString_(text)
        self.label.setFont_(AK.NSFont.systemFontOfSize_(13))
        self.label.setTextColor_(AK.NSColor.whiteColor())
        self.label.setSelectable_(True)
        self.label.setFocusRingType_(AK.NSFocusRingTypeNone)
        self.addSubview_(self.label)
        for path in self.files:
            icon = AK.NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 16, 16))
            icon.setImage_(AK.NSWorkspace.sharedWorkspace().iconForFile_(path))
            label = AK.NSTextField.labelWithString_(Path(path).name)
            label.setFont_(AK.NSFont.systemFontOfSize_(11))
            label.setTextColor_(AK.NSColor.colorWithWhite_alpha_(0.85, 1))
            label.setLineBreakMode_(AK.NSLineBreakByTruncatingMiddle)
            label.setToolTip_(path)
            label.setAccessibilityLabel_(f"Attachment: {Path(path).name}")
            self.addSubview_(icon)
            self.addSubview_(label)
            self.file_rows.append((icon, label))

    @objc.python_method
    def measure(self, max_width):
        paragraph = AK.NSMutableParagraphStyle.alloc().init()
        paragraph.setLineSpacing_(3)
        paragraph.setLineBreakMode_(AK.NSLineBreakByWordWrapping)
        value = NSAttributedString.alloc().initWithString_attributes_(self.text, {
            AK.NSFontAttributeName: self.label.font(),
            AK.NSForegroundColorAttributeName: AK.NSColor.whiteColor(),
            AK.NSParagraphStyleAttributeName: paragraph,
        })
        bounds = value.boundingRectWithSize_options_(
            NSMakeSize(max_width - 28, 1_000_000),
            AK.NSStringDrawingUsesLineFragmentOrigin | AK.NSStringDrawingUsesFontLeading,
        )
        file_width = max((len(Path(path).name) * 6 + 62 for path in self.files), default=0)
        width = min(max_width, max(math.ceil(bounds.size.width) + 32, file_width))
        self.label.setAttributedStringValue_(value)
        # The native cell owns wrapping and its extra text insets.
        cell_size = self.label.cell().cellSizeForBounds_(NSMakeRect(0, 0, width - 28, 1_000_000))
        text_height = math.ceil(cell_size.height) if self.text else 0
        file_height = len(self.files) * 28
        height = text_height + file_height + 22
        self.text_height = text_height
        self.layout_contents(width, height)
        return width, height

    @objc.python_method
    def layout_contents(self, width, height):
        Q.CATransaction.begin()
        Q.CATransaction.setDisableActions_(True)
        self.label.setFrame_(NSMakeRect(14, height - 11 - self.text_height, max(1, width - 28), self.text_height))
        for index, (icon, label) in enumerate(self.file_rows):
            y = 11 + (len(self.file_rows) - index - 1) * 28
            icon.setFrame_(NSMakeRect(14, y + 4, 16, 16))
            label.setFrame_(NSMakeRect(38, y + 3, width - 52, 20))
        if self.gradient:
            self.gradient.setFrame_(NSMakeRect(0, 0, width, height))
        Q.CATransaction.commit()


class AttachmentChip(AK.NSView):
    @objc.python_method
    def build(self, path, index, target):
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(9)
        self.layer().setBackgroundColor_(AK.NSColor.colorWithWhite_alpha_(0.15, 1).CGColor())
        width = min(210, max(110, len(Path(path).name) * 6 + 58))
        self.setFrame_(NSMakeRect(0, 0, width, 30))
        icon = AK.NSImageView.alloc().initWithFrame_(NSMakeRect(8, 7, 16, 16))
        icon.setImage_(AK.NSWorkspace.sharedWorkspace().iconForFile_(path))
        self.addSubview_(icon)
        label = PlaceholderField.labelWithString_(Path(path).name)
        label.setFont_(AK.NSFont.systemFontOfSize_(11))
        label.setTextColor_(AK.NSColor.whiteColor())
        label.setLineBreakMode_(AK.NSLineBreakByTruncatingMiddle)
        label.setFrame_(NSMakeRect(30, 7, width - 56, 17))
        self.addSubview_(label)
        remove = icon_button("xmark", f"Remove {Path(path).name}", target, "removeAttachment:")
        remove.setFrame_(NSMakeRect(width - 24, 5, 20, 20))
        remove.setTag_(index)
        self.addSubview_(remove)
        self.setToolTip_(path)


class HistoryRow(AK.NSButton):
    @objc.python_method
    def build(self, item, active, target):
        self.chat_id = item["id"]
        self.setTitle_("")
        self.setBordered_(False)
        self.setTarget_(target)
        self.setAction_("selectChat:")
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(12)
        self.layer().setBackgroundColor_(
            AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.13, 0.12, 0.20, 1).CGColor()
            if active else AK.NSColor.colorWithWhite_alpha_(0.07, 1).CGColor(),
        )
        self.setAccessibilityLabel_(f"Open chat: {item['title']}")
        self.title_label = PlaceholderField.labelWithString_(item["title"])
        self.title_label.setFont_(AK.NSFont.systemFontOfSize_weight_(13, AK.NSFontWeightMedium))
        self.title_label.setTextColor_(AK.NSColor.whiteColor())
        self.preview = PlaceholderField.labelWithString_(" ".join((item["draft"] or item["preview"] or "Start a conversation").split()))
        self.preview.setFont_(AK.NSFont.systemFontOfSize_(11))
        self.preview.setTextColor_(AK.NSColor.colorWithWhite_alpha_(0.52, 1))
        for label in (self.title_label, self.preview):
            label.setLineBreakMode_(AK.NSLineBreakByTruncatingTail)
            self.addSubview_(label)

    @objc.python_method
    def layout_row(self, width):
        self.title_label.setFrame_(NSMakeRect(14, 12, width - 28, 19))
        self.preview.setFrame_(NSMakeRect(14, 36, width - 28, 17))


class HistoryContent(NSObject):
    @objc.python_method
    def build(self, owner):
        self.owner = owner
        self.view = AK.NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 488, 384))
        self.view.setDrawsBackground_(False)
        self.view.setHasVerticalScroller_(True)
        self.view.setAutohidesScrollers_(True)
        self.view.setScrollerStyle_(AK.NSScrollerStyleOverlay)
        self.view.verticalScroller().setKnobStyle_(AK.NSScrollerKnobStyleLight)
        self.document = ChatDocument.alloc().initWithFrame_(NSMakeRect(0, 0, 488, 384))
        self.view.setDocumentView_(self.document)
        self.rows = []
        self._layout = None

    @objc.python_method
    def refresh(self):
        for row in self.rows:
            row.removeFromSuperview()
        self.rows = []
        self._layout = None
        for item in self.owner.chat.store.conversations():
            row = HistoryRow.alloc().initWithFrame_(NSMakeRect(0, 0, 488, 66))
            row.build(item, item["id"] == self.owner.chat.store.active, self)
            self.rows.append(row)
            self.document.addSubview_(row)
        self.layout(self.view.frame().size.width, self.view.frame().size.height)

    @objc.python_method
    def layout(self, width, height):
        if self._layout == (width, height):
            return
        self._layout = (width, height)
        y = 8
        for row in self.rows:
            row.setFrame_(NSMakeRect(0, y, width, 66))
            row.layout_row(width)
            y += 76
        self.document.setFrame_(NSMakeRect(0, 0, width, max(height, y)))

    def selectChat_(self, sender):
        if self.owner.chat.select_chat(sender.chat_id):
            self.owner.history_open = False
            self.owner.full_chat = True
            self.owner.open_chat()


class ChatContent(NSObject):
    @objc.python_method
    def build(self, owner, store=None):
        self.owner = owner
        self.store = store or ChatStore(":memory:")
        self.messages = []
        self.streams = {}
        self.files = []
        self.dirty = False
        self.status = ""
        self._layout = None
        self.message_time = time.monotonic()
        self.view = AK.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 56))
        self.scroll = AK.NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 68, 260, 200))
        self.scroll.setDrawsBackground_(False)
        self.scroll.setBorderType_(AK.NSNoBorder)
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.scroll.setScrollerStyle_(AK.NSScrollerStyleOverlay)
        self.scroll.verticalScroller().setKnobStyle_(AK.NSScrollerKnobStyleLight)
        self.document = ChatDocument.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 1))
        self.scroll.setDocumentView_(self.document)
        self.view.addSubview_(self.scroll)

        self.composer = AK.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 56))
        self.composer.setWantsLayer_(True)
        self.composer.layer().setCornerRadius_(14)
        self.composer.layer().setBackgroundColor_(AK.NSColor.colorWithWhite_alpha_(0.085, 1).CGColor())
        self.composer.layer().setBorderWidth_(1)
        self.composer.layer().setBorderColor_(AK.NSColor.colorWithWhite_alpha_(0.19, 1).CGColor())
        self.view.addSubview_(self.composer)
        self.input_scroll = AK.NSScrollView.alloc().initWithFrame_(NSMakeRect(14, 12, 192, 32))
        self.input_scroll.setDrawsBackground_(False)
        self.input_scroll.setBorderType_(AK.NSNoBorder)
        self.prompt = PromptView.alloc().initWithFrame_(self.input_scroll.bounds())
        self.prompt.owner = self
        self.prompt.setDelegate_(self)
        self.prompt.setRichText_(False)
        self.prompt.setImportsGraphics_(False)
        self.prompt.setAllowsUndo_(True)
        self.prompt.setDrawsBackground_(False)
        self.prompt.setFont_(AK.NSFont.systemFontOfSize_(13))
        self.prompt.setTextColor_(AK.NSColor.whiteColor())
        self.prompt.setInsertionPointColor_(AK.NSColor.whiteColor())
        self.prompt.setFocusRingType_(AK.NSFocusRingTypeNone)
        self.prompt.setAccessibilityLabel_("Message Luna")
        self.prompt.setTextContainerInset_(NSMakeSize(0, 5))
        self.prompt.setVerticallyResizable_(True)
        self.prompt.setHorizontallyResizable_(False)
        self.prompt.setMinSize_(NSMakeSize(0, 32))
        self.prompt.setMaxSize_(NSMakeSize(1_000_000, 1_000_000))
        self.prompt.setAutoresizingMask_(AK.NSViewWidthSizable)
        self.prompt.textContainer().setLineFragmentPadding_(0)
        self.prompt.textContainer().setContainerSize_(NSMakeSize(192, 1_000_000))
        self.prompt.textContainer().setWidthTracksTextView_(True)
        self.input_scroll.setDocumentView_(self.prompt)
        self.prompt.unregisterDraggedTypes()
        self.composer.addSubview_(self.input_scroll)
        self.placeholder = PlaceholderField.labelWithString_("Message Luna…")
        self.placeholder.setFont_(self.prompt.font())
        self.placeholder.setTextColor_(AK.NSColor.colorWithWhite_alpha_(0.46, 1))
        self.placeholder.setFrame_(NSMakeRect(14, 19, 185, 18))
        self.placeholder.setAccessibilityElement_(False)
        self.composer.addSubview_(self.placeholder)
        self.send_button = icon_button("arrow.up", "Send message", self, "send:")
        self.send_button.layer().setBackgroundColor_(BLUE.CGColor())
        self.composer.addSubview_(self.send_button)
        self.attachments_scroll = AK.NSScrollView.alloc().initWithFrame_(NSMakeRect(12, 60, 236, 32))
        self.attachments_scroll.setDrawsBackground_(False)
        self.attachments_scroll.setHasHorizontalScroller_(True)
        self.attachments_scroll.setAutohidesScrollers_(True)
        self.attachments_scroll.setScrollerStyle_(AK.NSScrollerStyleOverlay)
        self.attachments_document = AK.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 236, 32))
        self.attachments_scroll.setDocumentView_(self.attachments_document)
        self.composer.addSubview_(self.attachments_scroll)
        self.status_label = AK.NSTextField.labelWithString_("")
        self.status_label.setFont_(AK.NSFont.systemFontOfSize_(11))
        self.status_label.setTextColor_(AK.NSColor.colorWithWhite_alpha_(0.65, 1))
        self.composer.addSubview_(self.status_label)
        self._load_chat()

    @property
    def composer_height(self):
        return 56 + (40 if self.files else 0) + (18 if self.status else 0)

    @property
    def pending(self):
        stream = self.streams.get(self.store.active)
        if stream:
            message_id, text, start = stream
            bubble = next((b for b in self.messages if getattr(b, "message_id", None) == message_id), None)
            if bubble is not None:
                return bubble, text, start
        return None

    @objc.python_method
    def layout(self, width, height, full, *, animate=False, composer_height=None):
        self.scroll.setHidden_(not full)
        composer_height = self.composer_height if composer_height is None else composer_height
        self.composer.setFrame_(NSMakeRect(0, 0, width, composer_height))
        self.input_scroll.setFrame_(NSMakeRect(14, 12, max(1, width - 68), 32))
        self.placeholder.setFrame_(NSMakeRect(14, 19, max(1, width - 74), 18))
        self.send_button.setFrame_(NSMakeRect(max(0, width - 42), 14, 28, 28))
        self.attachments_scroll.setFrame_(NSMakeRect(12, 60, max(1, width - 24), 32))
        self.status_label.setFrame_(NSMakeRect(14, composer_height - 19, max(1, width - 28), 16))
        self.status_label.setHidden_(not self.status)
        self.status_label.setStringValue_(self.status)
        # Keep transcript wrapping stable while the island grows around it.
        transcript_width = self.owner.chat_width - 32
        transcript_height = max(1, self.owner.chat_height - self.owner.height - self.composer_height - 36)
        self.scroll.setFrame_(NSMakeRect(0, composer_height + 12, transcript_width, transcript_height))
        key = (transcript_width, transcript_height)
        if self._layout != key:
            self._layout = key
            self.layout_messages()

    @objc.python_method
    def layout_messages(self, *, force_bottom=False):
        viewport = self.scroll.contentView().bounds()
        at_bottom = self.document.frame().size.height - viewport.origin.y - viewport.size.height < 36
        width, y = viewport.size.width, 8
        for bubble in self.messages:
            bw, bh = bubble.measure(max(80, width * 0.84))
            x = width - bw - 4 if bubble.role == "user" else 4
            target = (x, y, bw, bh)
            if bubble.motion is None:
                bubble.motion = Spring(target)
            else:
                bubble.motion.move_to(target, immediate=AK.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
            bubble.setFrame_(NSMakeRect(*bubble.motion.value))
            bubble.layout_contents(bubble.motion.value[2], bubble.motion.value[3])
            y += bh + 12
        self.document.setFrame_(NSMakeRect(0, 0, width, max(viewport.size.height, y)))
        if at_bottom or force_bottom:
            destination = (0, max(0, y - viewport.size.height))
            if force_bottom or AK.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion():
                self.document.scrollPoint_(destination)
            elif abs(destination[1] - viewport.origin.y) > 0.1:
                AK.NSAnimationContext.beginGrouping()
                AK.NSAnimationContext.currentContext().setDuration_(0.16)
                self.scroll.contentView().animator().setBoundsOrigin_(destination)
                AK.NSAnimationContext.endGrouping()
            self.scroll.reflectScrolledClipView_(self.scroll.contentView())

    def textDidChange_(self, notification):
        has_text = bool(self.prompt.string().strip()) or bool(self.files)
        self.placeholder.setHidden_(bool(self.prompt.string()))
        self.send_button.setEnabled_(has_text and self.pending is None)
        self.send_button.setAlphaValue_(1 if has_text and self.pending is None else 0.35)
        self.dirty = True
        self.edited_at = time.monotonic()

    def textDidBeginEditing_(self, notification):
        self.composer.layer().setBorderColor_(BLUE.CGColor())

    def textDidEndEditing_(self, notification):
        self.composer.layer().setBorderColor_(AK.NSColor.colorWithWhite_alpha_(0.19, 1).CGColor())

    @objc.python_method
    def append(self, role, text, files=(), message_id=None):
        bubble = Bubble.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
        bubble.build(role, text, files)
        bubble.message_id = message_id
        self.messages.append(bubble)
        self.document.addSubview_(bubble)
        if not AK.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion():
            animation = Q.CABasicAnimation.animationWithKeyPath_("opacity")
            animation.setFromValue_(0)
            animation.setToValue_(1)
            animation.setDuration_(0.2)
            bubble.layer().addAnimation_forKey_(animation, "arrival")
        return bubble

    @objc.python_method
    def _load_chat(self):
        for bubble in self.messages:
            bubble.removeFromSuperview()
        self.messages = []
        self.status = ""
        row = self.store.current
        self.files = row["files"]
        self.prompt.setString_(row["draft"])
        for message in self.store.messages():
            self.append(message["role"], message["text"], message["files"], message["id"])
        self._rebuild_attachments()
        self.textDidChange_(None)
        self.dirty = False
        self._layout = None
        self.tick(time.monotonic())
        self.layout_messages(force_bottom=True)

    @objc.python_method
    def flush_draft(self, *, force=False):
        if self.dirty or force:
            try:
                self.store.save_draft(self.prompt.string(), self.files)
            except (sqlite3.Error, OSError):
                self.set_status("Could not save this draft. Your text is still here.")
                return False
            self.dirty = False
        return True

    @objc.python_method
    def select_chat(self, chat_id):
        if not self.flush_draft(force=True):
            return False
        self.store.select(chat_id)
        self._load_chat()
        return True

    @objc.python_method
    def new_chat(self):
        if not self.flush_draft(force=True):
            return False
        self.store.new()
        self._load_chat()
        return True

    @objc.python_method
    def set_status(self, text):
        self.status = text
        if hasattr(self.owner, "motion"):
            self.owner._draw(animate=True)

    @objc.python_method
    def _rebuild_attachments(self):
        for child in list(self.attachments_document.subviews()):
            child.removeFromSuperview()
        x = 0
        for index, path in enumerate(self.files):
            chip = AttachmentChip.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
            chip.build(path, index, self)
            chip.setFrameOrigin_((x, 0))
            self.attachments_document.addSubview_(chip)
            x += chip.frame().size.width + 6
        self.attachments_document.setFrame_(NSMakeRect(0, 0, max(1, x), 32))
        self.attachments_scroll.setHidden_(not self.files)

    @objc.python_method
    def attach_paths(self, paths):
        valid, rejected = 0, 0
        for value in paths:
            try:
                path = Path(value).resolve(strict=True)
                if not path.is_file() or not os.access(path, os.R_OK):
                    raise ValueError("Not a readable file")
            except (OSError, ValueError, RuntimeError):
                rejected += 1
                continue
            valid += 1
            if str(path) not in self.files:
                self.files.append(str(path))
        self._rebuild_attachments()
        self.textDidChange_(None)
        self.status = f"Skipped {rejected} unavailable file(s)." if rejected else ""
        self.owner.open_chat()
        self.owner._draw(animate=True)
        return valid > 0

    def removeAttachment_(self, sender):
        index = sender.tag()
        if 0 <= index < len(self.files):
            self.files.pop(index)
            self._rebuild_attachments()
            self.textDidChange_(None)
            self.status = ""
            self.owner._draw(animate=True)

    def send_(self, sender):
        text = self.prompt.string().strip()
        if (not text and not self.files) or self.pending:
            return
        if self.files and any(not Path(path).is_file() or not os.access(path, os.R_OK) for path in self.files):
            self.set_status("A file is no longer available. Remove it or drop it again.")
            return
        responses = (
            "I'm here. We can work through that together, one step at a time. What would you like to focus on first?",
            "Let's make it simple. Start with what matters most, leave a little room to think, and we can shape the details as we go.",
            "Of course. Tell me what a good result would look like, and we'll work toward it together.",
        )
        reply_text = responses[(len(self.messages) // 2) % len(responses)]
        try:
            user_id, reply_id = self.store.send(text, self.files, reply_text)
        except (sqlite3.Error, OSError):
            self.set_status("Could not save this message. Please try again.")
            return
        self.append("user", text, self.files, user_id)
        self.append("luna", "…", message_id=reply_id)
        self.streams[self.store.active] = (reply_id, reply_text, time.monotonic() + 0.35)
        self.prompt.setString_("")
        self.files = []
        self.status = ""
        self._rebuild_attachments()
        self.textDidChange_(None)
        self.dirty = False
        self.owner.full_chat = True
        self.owner.open_chat()
        self.owner._draw(animate=True)
        self.layout_messages(force_bottom=True)
        self.owner.panel.makeFirstResponder_(self.prompt)

    @objc.python_method
    def tick(self, now):
        elapsed = max(0, now - self.message_time)
        self.message_time = now
        if self.dirty and now - self.edited_at >= 0.3:
            self.flush_draft()
        if self.pending:
            bubble, text, start = self.pending
            count = max(0, min(len(text), int((now - start) * 75)))
            value = text[:count] or "…"
            if bubble.text != value:
                bubble.text = value
                self.layout_messages()
            if count == len(text):
                self.streams.pop(self.store.active, None)
                self.textDidChange_(None)
        for chat_id, (_, text, start) in list(self.streams.items()):
            if chat_id != self.store.active and now >= start + len(text) / 75:
                self.streams.pop(chat_id)
        for bubble in self.messages:
            if bubble.motion and bubble.motion.moving:
                x, y, width, height = bubble.motion.advance(elapsed)
                bubble.setFrame_(NSMakeRect(x, y, width, height))
                bubble.layout_contents(width, height)

    @objc.python_method
    def close(self):
        saved = self.flush_draft(force=True)
        self.store.close()
        if not saved:
            raise RuntimeError("Luna could not save the text-chat draft.")
