"""Native glass chat controls, local history, and replies streamed from the text agent."""

from datetime import datetime
import math
import os
from pathlib import Path
import sqlite3
import time

import AppKit as AK
from Foundation import NSAttributedString, NSMakeRect, NSMakeSize, NSObject
import objc
import Quartz as Q

from src.agent.text import TextRequest
from src.ui.chats import ChatStore
from src.ui.motion import Spring


def white(alpha):
    return AK.NSColor.colorWithWhite_alpha_(1, alpha)


AMBER = AK.NSColor.colorWithSRGBRed_green_blue_alpha_(1, 0.82, 0.54, 1)
GREEN = AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.55, 0.88, 0.71, 1)
VIOLET = AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.65, 0.58, 1, 1)
INSET = 12  # Text inset inside the island body; with PAD this puts every text column 24 pt from the edge.
ROW = 44
FIELD = 40
GENERIC_FAILURE = "Luna couldn't finish that reply."


def reply_text(text, status):
    if text or status == "failed":
        return text
    return {"pending": "…", "interrupted": "Reply interrupted."}.get(status, "Done, with nothing more to add.")


def steps_title(steps):
    services = list(dict.fromkeys(service for service, _ in steps if service))
    count = f"{len(steps)} step{'s' if len(steps) != 1 else ''}"
    return f"Used {', '.join(services)} · {count}" if services else f"Used tools · {count}"


def symbol(name, color=None, size=None, weight=AK.NSFontWeightRegular):
    image = AK.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    config = AK.NSImageSymbolConfiguration.configurationWithPaletteColors_([color or AK.NSColor.whiteColor()])
    if size:
        config = config.configurationByApplyingConfiguration_(
            AK.NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, weight))
    return image.imageWithSymbolConfiguration_(config)


def text_attributes(size, color, weight=AK.NSFontWeightRegular):
    return {AK.NSFontAttributeName: AK.NSFont.systemFontOfSize_weight_(size, weight),
            AK.NSForegroundColorAttributeName: color}


def inline_symbol(name, color, size, font, weight=AK.NSFontWeightSemibold):
    """An SF Symbol as a text attachment, vertically centred on the font's cap height."""
    image = symbol(name, color, size, weight)
    attachment = AK.NSTextAttachment.alloc().init()
    attachment.setImage_(image)
    width, height = image.size().width, image.size().height
    attachment.setBounds_(NSMakeRect(0, (font.capHeight() - height) / 2, width, height))
    return NSAttributedString.attributedStringWithAttachment_(attachment)


def rich(parts, size, color, weight=AK.NSFontWeightRegular):
    """Join text and (symbol name, colour) parts into one attributed string."""
    font = AK.NSFont.systemFontOfSize_weight_(size, weight)
    value = AK.NSMutableAttributedString.alloc().init()
    for part in parts:
        if isinstance(part, tuple):
            value.appendAttributedString_(inline_symbol(part[0], part[1], size - 3, font))
        else:
            value.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
                part, {AK.NSFontAttributeName: font, AK.NSForegroundColorAttributeName: color}))
    return value


def emphasize(text, query, size, color, strong):
    """Text with every case-insensitive match of `query` in bold and the strong colour."""
    value = AK.NSMutableAttributedString.alloc().initWithString_attributes_(text, text_attributes(size, color))
    if query:
        lower, needle, at = text.lower(), query.lower(), 0
        while (at := lower.find(needle, at)) >= 0:
            value.addAttributes_range_(text_attributes(size, strong, AK.NSFontWeightSemibold), (at, len(needle)))
            at += len(needle)
    return value


def label(text, size, color, weight=AK.NSFontWeightRegular):
    field = PlaceholderField.labelWithString_(text)
    field.setFont_(AK.NSFont.systemFontOfSize_weight_(size, weight))
    field.setTextColor_(color)
    field.setLineBreakMode_(AK.NSLineBreakByTruncatingTail)
    return field


def glass(view, radius, fill=0.07, border=0.12):
    view.setWantsLayer_(True)
    view.layer().setCornerRadius_(radius)
    view.layer().setBackgroundColor_(white(fill).CGColor())
    view.layer().setBorderWidth_(1 if border else 0)
    view.layer().setBorderColor_(white(border).CGColor())
    return view


# The panel is rarely key and Luna is never the active app, so AppKit's cursor rects and cursor-update areas
# (key window or active app only) never fire. Always-active enter/exit areas set hover cursors instead;
# one owner at a time holds the cursor, so leaving or hiding can't strand a stale one.
_cursor_owner = None


def track_cursor(view, owner):
    view.addTrackingArea_(AK.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
        NSMakeRect(0, 0, 0, 0),
        AK.NSTrackingMouseEnteredAndExited | AK.NSTrackingActiveAlways | AK.NSTrackingInVisibleRect,
        owner, None,
    ))


def enter_cursor(owner, cursor):
    global _cursor_owner
    _cursor_owner = owner
    cursor.set()


def exit_cursor(owner):
    if _cursor_owner is owner:
        release_cursor()


def release_cursor():
    global _cursor_owner
    if _cursor_owner is not None:
        _cursor_owner = None
        AK.NSCursor.arrowCursor().set()


HOVER = white(0.12)


class PointerButton(AK.NSButton):
    """A button with a pointing hand and a fading glass highlight on hover, without the panel being key."""

    def initWithFrame_(self, frame):
        self = objc.super(PointerButton, self).initWithFrame_(frame)
        if self is not None:
            track_cursor(self, self)
        return self

    def mouseEntered_(self, event):
        objc.super(PointerButton, self).mouseEntered_(event)
        if self.isEnabled():
            enter_cursor(self, AK.NSCursor.pointingHandCursor())
            self.set_hover(True)

    def mouseExited_(self, event):
        objc.super(PointerButton, self).mouseExited_(event)
        self.leave()

    def setEnabled_(self, enabled):
        objc.super(PointerButton, self).setEnabled_(enabled)
        if not enabled:
            self.leave()

    def viewDidHide(self):
        objc.super(PointerButton, self).viewDidHide()
        self.leave()

    def viewWillMoveToWindow_(self, window):
        objc.super(PointerButton, self).viewWillMoveToWindow_(window)
        if window is None:
            self.leave()

    @objc.python_method
    def leave(self):
        exit_cursor(self)
        self.set_hover(False)

    @objc.python_method
    def set_hover(self, on):
        layer = self.layer()
        if layer is None:
            return
        # An overlay keeps state colours (pinned, selected, Send) underneath and brightens them on hover.
        overlay = getattr(self, "hover_layer", None)
        if overlay is None:
            if not on:
                return
            overlay = self.hover_layer = Q.CALayer.layer()
            overlay.setBackgroundColor_(HOVER.CGColor())
            overlay.setOpacity_(0)
            layer.addSublayer_(overlay)
        overlay.setFrame_(layer.bounds())
        overlay.setCornerRadius_(layer.cornerRadius())
        target = 1.0 if on else 0.0
        if overlay.opacity() == target:
            return
        if not AK.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion():
            fade = Q.CABasicAnimation.animationWithKeyPath_("opacity")
            current = overlay.presentationLayer()
            fade.setFromValue_(current.opacity() if current is not None else overlay.opacity())
            fade.setToValue_(target)
            fade.setDuration_(0.16)
            overlay.addAnimation_forKey_(fade, "hover")
        overlay.setOpacity_(target)


def icon_button(name, label_text, target, action, *, size=30, radius=10, color=None, point=14):
    button = PointerButton.alloc().initWithFrame_(NSMakeRect(0, 0, size, size))
    button.setTitle_("")
    button.setBordered_(False)
    button.setImage_(symbol(name, color or white(0.78), point))
    button.setImageScaling_(AK.NSImageScaleNone)
    button.setToolTip_(label_text)
    button.setAccessibilityLabel_(label_text)
    button.setTarget_(target)
    button.setAction_(action)
    button.setWantsLayer_(True)
    button.layer().setCornerRadius_(radius)
    return button


def text_button(title, image, target, action, *, size=12, weight=AK.NSFontWeightRegular, color=None, height=28):
    button = PointerButton.alloc().initWithFrame_(NSMakeRect(0, 0, 80, height))
    button.setBordered_(False)
    button.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
        title, text_attributes(size, color or white(0.84), weight)))
    if image:
        button.setImage_(image)
        button.setImagePosition_(AK.NSImageLeading)
    button.cell().setLineBreakMode_(AK.NSLineBreakByTruncatingTail)
    # Keep icons next to their label instead of pinned to the button's edge.
    button.cell().setImageHugsTitle_(True)
    button.setTarget_(target)
    button.setAction_(action)
    button.setWantsLayer_(True)
    button.layer().setCornerRadius_(height / 2)
    return button


def key_hint(title, target=None, action=None, label_text=None):
    """A keycap: idle it is outlined and tertiary; live it takes the selected fill and white text."""
    cap = PointerButton.alloc().initWithFrame_(NSMakeRect(0, 0, 26, 22))
    cap.setBordered_(False)
    cap.setWantsLayer_(True)
    cap.layer().setCornerRadius_(6)
    cap.setTarget_(target)
    cap.setAction_(action)
    cap.setAccessibilityLabel_(label_text or title)
    cap.key_title = title
    set_key_live(cap, False)
    return cap


def set_key_live(cap, live):
    cap.live = live
    cap.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
        cap.key_title, text_attributes(11, AK.NSColor.whiteColor() if live else white(0.48), AK.NSFontWeightSemibold)))
    cap.layer().setBackgroundColor_(white(0.16 if live else 0).CGColor())
    cap.layer().setBorderWidth_(0 if live else 1)
    cap.layer().setBorderColor_(white(0.14).CGColor())


class Rule(AK.NSView):
    """The hairline under a field: white 14% at rest, a violet fade while it holds text or focus."""

    @objc.python_method
    def build(self):
        self.setWantsLayer_(True)
        self.fill = Q.CAGradientLayer.layer()
        self.fill.setStartPoint_((0, 0.5))
        self.fill.setEndPoint_((1, 0.5))
        self.layer().addSublayer_(self.fill)
        self.set_live(False)
        return self

    @objc.python_method
    def set_live(self, live):
        self.live = live
        colors = ([VIOLET.CGColor(), VIOLET.colorWithAlphaComponent_(0.15).CGColor()] if live
                  else [white(0.14).CGColor(), white(0.14).CGColor()])
        Q.CATransaction.begin()
        Q.CATransaction.setDisableActions_(True)
        self.fill.setColors_(colors)
        self.fill.setFrame_(self.bounds())
        Q.CATransaction.commit()

    def setFrame_(self, frame):
        objc.super(Rule, self).setFrame_(frame)
        if getattr(self, "fill", None) is not None:
            self.fill.setFrame_(self.bounds())


class ChatDocument(AK.NSView):
    def isFlipped(self):
        return True


class PlaceholderField(AK.NSTextField):
    def hitTest_(self, point):
        return None


class PromptView(AK.NSTextView):
    def needsPanelToBecomeKey(self):
        return True

    def acceptsFirstMouse_(self, event):
        # The panel is never key before this click; without first mouse it only keys the panel and drops the click.
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


class Bubble(ChatDocument):
    """Your turns are surface cards; Luna's are plain text with a quiet step line and, on failure, an inline error."""

    @objc.python_method
    def build(self, role, text, files=(), steps=(), error=None):
        self.role, self.text = role, text
        self.files = tuple(files)
        self.file_rows = []
        self.motion = None
        self.owner = None
        self.steps, self.steps_open, self.error = (), False, None
        if role == "user":
            glass(self, 14, 0.06, 0.10)
        self.label = AK.NSTextField.wrappingLabelWithString_(text)
        self.label.setFont_(AK.NSFont.systemFontOfSize_(13.5))
        self.label.setTextColor_(AK.NSColor.whiteColor() if role == "user" else white(0.94))
        self.label.setSelectable_(True)
        self.label.setFocusRingType_(AK.NSFocusRingTypeNone)
        self.addSubview_(self.label)
        for path in self.files:
            icon = AK.NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 16, 16))
            icon.setImage_(AK.NSWorkspace.sharedWorkspace().iconForFile_(path))
            name = label(Path(path).name, 12, white(0.85))
            name.setLineBreakMode_(AK.NSLineBreakByTruncatingMiddle)
            name.setToolTip_(path)
            name.setAccessibilityLabel_(f"Attachment: {Path(path).name}")
            self.addSubview_(icon)
            self.addSubview_(name)
            self.file_rows.append((icon, name))
        if role == "luna":
            self.steps_button = text_button("", None, self, "toggleSteps:", height=24)
            self.steps_button.setAlignment_(AK.NSTextAlignmentLeft)
            self.steps_button.layer().setCornerRadius_(8)
            self.addSubview_(self.steps_button)
            self.step_labels = []
            self.error_label = AK.NSTextField.wrappingLabelWithString_("")
            self.error_label.setSelectable_(False)
            self.addSubview_(self.error_label)
            self.retry_button = text_button("Try again", None, self, "retry:", size=12.5,
                                            color=AK.NSColor.whiteColor(), height=28)
            self.retry_button.setAlignment_(AK.NSTextAlignmentLeft)
            self.retry_button.layer().setCornerRadius_(8)
            self.addSubview_(self.retry_button)
            self.update(text, steps, error)

    @objc.python_method
    def update(self, text, steps=(), error=None):
        self.text = text
        self.error = error
        steps = tuple(tuple(s) for s in steps)
        if steps != self.steps:
            self.steps = steps
            for item in self.step_labels:
                item.removeFromSuperview()
            self.step_labels = [label(text, 12.5, white(0.48)) for _, text in steps]
            for item in self.step_labels:
                self.addSubview_(item)
        self._steps_title()
        self.error_label.setAttributedStringValue_(rich(
            [("exclamationmark.circle", AMBER), "  " + (error or "")], 12.5, AMBER))

    @objc.python_method
    def _steps_title(self):
        title = steps_title(self.steps) if self.steps else ""
        self.steps_button.setAttributedTitle_(rich(
            [("checkmark", GREEN), "  " + title + "  ", ("chevron.down" if self.steps_open else "chevron.right",
                                                       white(0.48))], 12.5, white(0.64)))
        self.steps_button.setAccessibilityLabel_(
            f"{title}, {'collapse' if self.steps_open else 'expand'}" if self.steps else "")

    def toggleSteps_(self, sender):
        self.steps_open = not self.steps_open
        self._steps_title()
        if self.owner is not None:
            self.owner.layout_messages()

    def retry_(self, sender):
        if self.owner is not None:
            self.owner.retry(self)

    @objc.python_method
    def measure(self, max_width):
        paragraph = AK.NSMutableParagraphStyle.alloc().init()
        paragraph.setLineSpacing_(3.5)
        paragraph.setLineBreakMode_(AK.NSLineBreakByWordWrapping)
        value = NSAttributedString.alloc().initWithString_attributes_(self.text, {
            AK.NSFontAttributeName: self.label.font(),
            AK.NSForegroundColorAttributeName: self.label.textColor(),
            AK.NSParagraphStyleAttributeName: paragraph,
        })
        self.label.setAttributedStringValue_(value)
        if self.role == "luna":
            width = max_width
            cell = self.label.cell().cellSizeForBounds_(NSMakeRect(0, 0, width, 1_000_000))
            self.text_height = math.ceil(cell.height) if self.text else 0
            self.steps_height = (24 + (len(self.steps) * 20 + 4 if self.steps_open else 0)) if self.steps else 0
            height = (self.steps_height + 6 if self.steps else 0) + self.text_height
            self.error_height = 0
            if self.error:
                cell = self.error_label.cell().cellSizeForBounds_(NSMakeRect(0, 0, width, 1_000_000))
                self.error_height = math.ceil(cell.height)
                height += (8 if height else 0) + self.error_height + 4 + 28
            self.layout_contents(width, max(height, 18))
            return width, max(height, 18)
        bounds = value.boundingRectWithSize_options_(
            NSMakeSize(max_width - 26, 1_000_000),
            AK.NSStringDrawingUsesLineFragmentOrigin | AK.NSStringDrawingUsesFontLeading,
        )
        file_width = max((len(Path(path).name) * 6.5 + 62 for path in self.files), default=0)
        width = min(max_width, max(math.ceil(bounds.size.width) + 30, file_width))
        # The native cell owns wrapping and its extra text insets.
        cell = self.label.cell().cellSizeForBounds_(NSMakeRect(0, 0, width - 26, 1_000_000))
        self.text_height = math.ceil(cell.height) if self.text else 0
        height = self.text_height + len(self.files) * 28 + 18
        self.layout_contents(width, height)
        return width, height

    @objc.python_method
    def layout_contents(self, width, height):
        Q.CATransaction.begin()
        Q.CATransaction.setDisableActions_(True)
        if self.role == "luna":
            y = 0
            self.steps_button.setHidden_(not self.steps)
            if self.steps:
                # Flush with the reply's text column; the hover fill extends to the right only.
                self.steps_button.setFrame_(NSMakeRect(0, 0, min(width, self.steps_button.fittingSize().width + 8), 24))
                for index, item in enumerate(self.step_labels):
                    item.setHidden_(not self.steps_open)
                    item.setFrame_(NSMakeRect(20, 26 + index * 20, max(1, width - 20), 17))
                y = self.steps_height + 6
            self.label.setHidden_(not self.text)
            # Labels draw 2 pt inside their frame; this keeps Luna's text on the field's column.
            self.label.setFrame_(NSMakeRect(-2, y, max(1, width + 2), self.text_height))
            y += self.text_height
            self.error_label.setHidden_(not self.error)
            self.retry_button.setHidden_(not self.error)
            if self.error:
                y += 8 if y else 0
                self.error_label.setFrame_(NSMakeRect(0, y, max(1, width), self.error_height))
                self.retry_button.setFrame_(NSMakeRect(0, y + self.error_height + 4,
                                                       self.retry_button.fittingSize().width + 8, 28))
        else:
            self.label.setFrame_(NSMakeRect(13, 9, max(1, width - 26), self.text_height))
            for index, (icon, name) in enumerate(self.file_rows):
                y = 9 + self.text_height + index * 28
                icon.setFrame_(NSMakeRect(13, y + 4, 16, 16))
                name.setFrame_(NSMakeRect(36, y + 3, width - 50, 20))
        Q.CATransaction.commit()


class AttachmentChip(AK.NSView):
    @objc.python_method
    def build(self, path, index, target):
        glass(self, 8, 0.06, 0.10)
        width = min(220, max(110, len(Path(path).name) * 6.5 + 60))
        self.setFrame_(NSMakeRect(0, 0, width, 28))
        icon = AK.NSImageView.alloc().initWithFrame_(NSMakeRect(8, 6, 16, 16))
        icon.setImage_(AK.NSWorkspace.sharedWorkspace().iconForFile_(path))
        self.addSubview_(icon)
        name = label(Path(path).name, 12, AK.NSColor.whiteColor())
        name.setLineBreakMode_(AK.NSLineBreakByTruncatingMiddle)
        name.setFrame_(NSMakeRect(30, 6, width - 56, 16))
        self.addSubview_(name)
        remove = icon_button("xmark", f"Remove {Path(path).name}", target, "removeAttachment:",
                             size=20, radius=6, point=9)
        remove.setFrame_(NSMakeRect(width - 24, 4, 20, 20))
        remove.setTag_(index)
        self.addSubview_(remove)
        self.setToolTip_(path)


def when(timestamp, now=None):
    moment, now = datetime.fromtimestamp(timestamp), now or datetime.now()
    if moment.date() == now.date():
        # Follow the user's 12/24-hour preference rather than a fixed format.
        formatter = AK.NSDateFormatter.alloc().init()
        formatter.setDateStyle_(AK.NSDateFormatterNoStyle)
        formatter.setTimeStyle_(AK.NSDateFormatterShortStyle)
        return formatter.stringFromDate_(AK.NSDate.dateWithTimeIntervalSince1970_(timestamp))
    if (now.date() - moment.date()).days == 1:
        return "Yesterday"
    if (now.date() - moment.date()).days < 7:
        return moment.strftime("%a")
    return f"{moment.day} {moment.strftime('%b')}"


class HistoryRow(PointerButton):
    """One result: the title (matches in bold), an optional snippet, and when; the open chat gets a violet dot."""

    @objc.python_method
    def build(self, item, query, snippet_text, current, target):
        self.chat_id = item["id"]
        self.setTitle_("")
        self.setBordered_(False)
        self.setTarget_(target)
        self.setAction_("selectChat:")
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(8)
        self.setAccessibilityLabel_(f"Open chat: {item['title']}")
        self.title_label = label("", 13.5, AK.NSColor.whiteColor())
        self.title_label.setAttributedStringValue_(emphasize(item["title"], query, 13.5, white(0.94),
                                                             AK.NSColor.whiteColor()))
        self.snippet = None
        if snippet_text:
            self.snippet = label("", 12, white(0.48))
            self.snippet.setAttributedStringValue_(emphasize(f"“{snippet_text}”", query, 12, white(0.48), white(0.9)))
            self.addSubview_(self.snippet)
        self.date = label(when(item["updated"]), 11.5, white(0.48))
        self.date.setAlignment_(AK.NSTextAlignmentRight)
        self.dot = AK.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 6, 6))
        self.dot.setWantsLayer_(True)
        self.dot.layer().setCornerRadius_(3)
        self.dot.layer().setBackgroundColor_(VIOLET.CGColor())
        self.dot.layer().setShadowColor_(VIOLET.CGColor())
        self.dot.layer().setShadowOpacity_(0.9)
        self.dot.layer().setShadowRadius_(4)
        self.dot.layer().setShadowOffset_((0, 0))
        self.dot.setHidden_(not current)
        for view in (self.title_label, self.date, self.dot):
            self.addSubview_(view)
        return ROW + (14 if snippet_text else 0)

    @objc.python_method
    def set_selected(self, selected):
        self.selected = selected
        self.layer().setBackgroundColor_(white(0.12 if selected else 0).CGColor())

    def isFlipped(self):
        return True

    @objc.python_method
    def layout_row(self, width, height):
        top = (height - (36 if self.snippet else 18)) / 2
        self.dot.setFrame_(NSMakeRect(-2, top + 6, 6, 6))
        self.title_label.setFrame_(NSMakeRect(INSET, top, width - INSET * 2 - 84, 18))
        if self.snippet:
            self.snippet.setFrame_(NSMakeRect(INSET, top + 20, width - INSET * 2, 16))
        self.date.setFrame_(NSMakeRect(width - INSET - 80, top + 1, 80, 16))


class SearchField(AK.NSTextField):
    def needsPanelToBecomeKey(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True


class HistoryContent(NSObject):
    """Typeahead history: the field becomes search; empty it shows the last three chats, typing searches all."""

    @objc.python_method
    def build(self, owner):
        self.owner = owner
        self.view = ChatDocument.alloc().initWithFrame_(NSMakeRect(0, 0, 396, 300))
        self.icon = AK.NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 18, 18))
        self.icon.setImage_(symbol("magnifyingglass", white(0.64), 14))
        self.view.addSubview_(self.icon)
        self.ghost = PlaceholderField.labelWithString_("")
        self.ghost.setFont_(AK.NSFont.systemFontOfSize_(15))
        self.ghost.setTextColor_(white(0.40))
        self.ghost.setAccessibilityElement_(False)
        self.view.addSubview_(self.ghost)
        self.search = SearchField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 22))
        self.search.setBezeled_(False)
        self.search.setDrawsBackground_(False)
        self.search.setFocusRingType_(AK.NSFocusRingTypeNone)
        self.search.setFont_(AK.NSFont.systemFontOfSize_(15))
        self.search.setTextColor_(AK.NSColor.whiteColor())
        self.search.setPlaceholderAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            "Search chats", text_attributes(15, white(0.48))))
        self.search.setAccessibilityLabel_("Search chats")
        self.search.setDelegate_(self)
        track_cursor(self.search, self)
        self.view.addSubview_(self.search)
        self.esc = key_hint("esc", owner, "closeHistory:", "Back to writing")
        self.view.addSubview_(self.esc)
        self.rule = Rule.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 1)).build()
        self.rule.set_live(True)
        self.view.addSubview_(self.rule)
        self.footer = label("", 11.5, white(0.48))
        self.footer_right = label("", 11.5, white(0.48))
        self.footer_right.setAlignment_(AK.NSTextAlignmentRight)
        for view in (self.footer, self.footer_right):
            self.view.addSubview_(view)
        self.rows, self.sections, self.items = [], [], []
        self.completion = ""
        self.selected = 0
        self.content_height = 0
        self._layout = None

    def mouseEntered_(self, event):
        enter_cursor(self, AK.NSCursor.IBeamCursor())

    def mouseExited_(self, event):
        exit_cursor(self)

    def controlTextDidChange_(self, notification):
        self.refresh()

    @objc.typedSelector(b"Z@:@@:")
    def control_textView_doCommandBySelector_(self, control, text_view, command):
        name = command.decode() if isinstance(command, bytes) else str(command)
        if name == "moveDown:":
            self.move(1)
        elif name == "moveUp:":
            self.move(-1)
        elif name == "insertNewline:":
            self.open_selected()
        elif name == "insertTab:" and self.completion:
            self.search.setStringValue_(self.search.stringValue() + self.completion)
            text_view.setSelectedRange_((len(self.search.stringValue()), 0))
            self.refresh()
        elif name == "cancelOperation:":
            self.owner.closeHistory_(None)
        else:
            return False
        return True

    @objc.python_method
    def move(self, step):
        if self.rows:
            self.selected = (self.selected + step) % len(self.rows)
            for index, row in enumerate(self.rows):
                row.set_selected(index == self.selected)

    @objc.python_method
    def open_selected(self):
        if self.rows:
            self.selectChat_(self.rows[self.selected])

    @objc.python_method
    def _completion(self, query):
        if not query:
            return ""
        for item in self.owner.chat.store.conversations():
            if item["title"].lower().startswith(query.lower()) and len(item["title"]) > len(query):
                return item["title"][len(query):]
        return ""

    @objc.python_method
    def refresh(self):
        for view in [*self.rows, *self.sections]:
            view.removeFromSuperview()
        self.rows, self.sections, self.items = [], [], []
        self._layout = None
        query = self.search.stringValue().strip()
        store, active = self.owner.chat.store, self.owner.chat.store.active
        groups = []
        if not query:
            groups.append((None, [(item, None) for item in store.conversations(limit=3)]))
        else:
            titles, mentions = store.search(query)
            if titles:
                groups.append(("Chat" if len(titles) == 1 else "Chats", [(item, None) for item in titles]))
            if mentions:
                groups.append(("Mentioned in", mentions))
        for caption, entries in groups:
            if caption:
                section = label(caption, 11.5, white(0.48))
                self.sections.append(section)
                self.items.append((section, 32 if self.items else 24))
                self.view.addSubview_(section)
            for item, snippet_text in entries:
                row = HistoryRow.alloc().initWithFrame_(NSMakeRect(0, 0, 396, ROW))
                height = row.build(item, query, snippet_text, item["id"] == active, self)
                self.rows.append(row)
                self.items.append((row, height))
                self.view.addSubview_(row)
        if not self.rows:
            empty = label(f"No chats mention “{query}”." if query else "Your chats will appear here.", 13,
                          white(0.64))
            self.sections.append(empty)
            self.items.append((empty, ROW))
            self.view.addSubview_(empty)
        self.selected = 0
        self.move(0)
        self.completion = self._completion(query) if query == self.search.stringValue() else ""
        self.ghost.setStringValue_(self.completion)
        self.footer.setAttributedStringValue_(rich(
            ["↑ ↓  Move     ⏎  Open" + ("     ⇥  Complete" if self.completion else "")], 11.5, white(0.48)))
        self.footer_right.setStringValue_("" if query else "Type to search every chat")
        self.content_height = 56 + 16 + sum(height for _, height in self.items) + 48
        self.layout(self.view.frame().size.width, self.view.frame().size.height)
        if getattr(self.owner, "mode", None) == "history":
            self.owner._draw(animate=True)  # The island follows the number of results.

    @objc.python_method
    def layout(self, width, height):
        if self._layout == (width, height):
            return
        self._layout = (width, height)
        self.icon.setFrame_(NSMakeRect(INSET, 19, 18, 18))
        field_x, field_width = INSET + 28, max(1, width - INSET * 2 - 28 - 36)
        self.search.setFrame_(NSMakeRect(field_x, 17, field_width, 22))
        typed = NSAttributedString.alloc().initWithString_attributes_(
            self.search.stringValue(), text_attributes(15, AK.NSColor.whiteColor())).size().width
        self.ghost.setFrame_(NSMakeRect(field_x + typed + 2, 17, max(1, field_width - typed), 22))
        self.esc.setFrame_(NSMakeRect(width - INSET - 30, 17, 30, 22))
        self.rule.setFrame_(NSMakeRect(INSET, 52, max(1, width - INSET * 2), 1))
        y = 52 + 16
        for view, item_height in self.items:
            if isinstance(view, HistoryRow):
                view.setFrame_(NSMakeRect(0, y, width, item_height))
                view.layout_row(width, item_height)
            else:
                view.setFrame_(NSMakeRect(INSET, y + item_height - 22, width - INSET * 2, 16))
            y += item_height + (2 if isinstance(view, HistoryRow) else 0)
        query = bool(self.search.stringValue().strip())
        self.footer.setFrame_(NSMakeRect(INSET, y + 16, (width - INSET * 2) if query else width / 2, 16))
        self.footer_right.setFrame_(NSMakeRect(width / 2, y + 16, width / 2 - INSET, 16))

    def selectChat_(self, sender):
        if self.owner.chat.select_chat(sender.chat_id):
            self.owner.history_open = False
            self.owner.full_chat = True
            self.owner.open_chat()


class ChatContent(NSObject):
    @objc.python_method
    def build(self, owner, store=None, submit=None):
        self.owner = owner
        self.store = store or ChatStore(":memory:")
        self.submit = submit or (lambda request: None)
        self.messages = []
        self.streams = {}
        self.files = []
        self.dirty = False
        self.status = ""
        self.attention = False
        self._layout = None
        self.message_time = time.monotonic()
        self.view = AK.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 396, 56))
        self.scroll = AK.NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 68, 396, 200))
        self.scroll.setDrawsBackground_(False)
        self.scroll.setBorderType_(AK.NSNoBorder)
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.scroll.setScrollerStyle_(AK.NSScrollerStyleOverlay)
        self.scroll.verticalScroller().setKnobStyle_(AK.NSScrollerKnobStyleLight)
        self.document = ChatDocument.alloc().initWithFrame_(NSMakeRect(0, 0, 396, 1))
        self.scroll.setDocumentView_(self.document)
        self.view.addSubview_(self.scroll)

        # Open field: 15 pt text straight on the glass, over a hairline. No box, no send button.
        self.input_scroll = AK.NSScrollView.alloc().initWithFrame_(NSMakeRect(INSET, 12, 300, 32))
        self.input_scroll.setDrawsBackground_(False)
        self.input_scroll.setBorderType_(AK.NSNoBorder)
        self.prompt = PromptView.alloc().initWithFrame_(self.input_scroll.bounds())
        self.prompt.owner = self
        self.prompt.setDelegate_(self)
        self.prompt.setRichText_(False)
        self.prompt.setImportsGraphics_(False)
        self.prompt.setAllowsUndo_(True)
        self.prompt.setDrawsBackground_(False)
        self.prompt.setFont_(AK.NSFont.systemFontOfSize_(15))
        self.prompt.setTextColor_(AK.NSColor.whiteColor())
        self.prompt.setInsertionPointColor_(VIOLET)
        self.prompt.setFocusRingType_(AK.NSFocusRingTypeNone)
        self.prompt.setAccessibilityLabel_("Message Luna")
        self.prompt.setTextContainerInset_(NSMakeSize(0, 6))
        self.prompt.setVerticallyResizable_(True)
        self.prompt.setHorizontallyResizable_(False)
        self.prompt.setMinSize_(NSMakeSize(0, 32))
        self.prompt.setMaxSize_(NSMakeSize(1_000_000, 1_000_000))
        self.prompt.setAutoresizingMask_(AK.NSViewWidthSizable)
        self.prompt.textContainer().setLineFragmentPadding_(0)
        self.prompt.textContainer().setContainerSize_(NSMakeSize(300, 1_000_000))
        self.prompt.textContainer().setWidthTracksTextView_(True)
        self.input_scroll.setDocumentView_(self.prompt)
        # NSTextView's own I-beam only tracks in the key window; hover never keys the panel.
        track_cursor(self.input_scroll, self)
        self.prompt.unregisterDraggedTypes()
        self.view.addSubview_(self.input_scroll)
        self.placeholder = label("Ask Luna anything", 15, white(0.48))
        self.placeholder.setAccessibilityElement_(False)
        self.view.addSubview_(self.placeholder)
        self.attach_button = icon_button("paperclip", "Attach files", self, "chooseFiles:", size=28, radius=8,
                                         color=white(0.64), point=14)
        self.think_button = icon_button("brain", "Think", self, "toggleThink:", size=28, radius=8,
                                        color=white(0.64), point=14)
        self.send_button = key_hint("⏎", self, "send:", "Send message")
        for view in (self.attach_button, self.think_button, self.send_button):
            self.view.addSubview_(view)
        self.rule = Rule.alloc().initWithFrame_(NSMakeRect(INSET, 4, 300, 1)).build()
        self.view.addSubview_(self.rule)
        self.attachments_scroll = AK.NSScrollView.alloc().initWithFrame_(NSMakeRect(INSET, 52, 300, 28))
        self.attachments_scroll.setDrawsBackground_(False)
        self.attachments_scroll.setHasHorizontalScroller_(True)
        self.attachments_scroll.setAutohidesScrollers_(True)
        self.attachments_scroll.setScrollerStyle_(AK.NSScrollerStyleOverlay)
        self.attachments_document = AK.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 28))
        self.attachments_scroll.setDocumentView_(self.attachments_document)
        self.view.addSubview_(self.attachments_scroll)
        self.status_label = label("", 11.5, AMBER)
        self.view.addSubview_(self.status_label)
        self._show_mode()
        self._load_chat()

    @property
    def composer_height(self):
        return 8 + FIELD + 4 + (36 if self.files else 0) + (22 if self.status else 0)

    @property
    def pending(self):
        """The active chat's streaming reply bubble, if one is still arriving."""
        message_id = self.streams.get(self.store.active)
        return self._bubble(message_id) if message_id is not None else None

    @objc.python_method
    def _bubble(self, message_id):
        return next((b for b in self.messages if getattr(b, "message_id", None) == message_id), None)

    @objc.python_method
    def set_placeholder(self, text):
        if self.placeholder.stringValue() != text:
            self.placeholder.setStringValue_(text)

    @objc.python_method
    def layout(self, width, height, full, *, animate=False, composer_height=None):
        self.scroll.setHidden_(not full)
        composer_height = self.composer_height if composer_height is None else composer_height
        inner = max(1, width - INSET * 2)
        self.rule.setFrame_(NSMakeRect(INSET, 4, inner, 1))
        self.send_button.setFrame_(NSMakeRect(width - INSET - 26, 17, 26, 22))
        self.think_button.setFrame_(NSMakeRect(width - INSET - 26 - 34, 14, 28, 28))
        self.attach_button.setFrame_(NSMakeRect(width - INSET - 26 - 66, 14, 28, 28))
        field_width = max(1, inner - 100)
        self.input_scroll.setFrame_(NSMakeRect(INSET, 10, field_width, 34))
        # Labels draw 2 pt inside their frame; offset so the placeholder starts where typed text does.
        self.placeholder.setFrame_(NSMakeRect(INSET - 2, 17, field_width, 20))
        self.attachments_scroll.setFrame_(NSMakeRect(INSET, 8 + FIELD + 8, inner, 28))
        self.status_label.setFrame_(NSMakeRect(INSET, composer_height - 18, inner, 16))
        self.status_label.setHidden_(not self.status)
        self.status_label.setStringValue_(self.status)
        # Keep transcript wrapping stable while the island grows around it.
        transcript_width = max(1, self.owner.transcript_width - INSET * 2)
        transcript_height = max(1, height - composer_height - 12)
        self.scroll.setFrame_(NSMakeRect(INSET, composer_height + 12, transcript_width, transcript_height))
        key = (transcript_width, transcript_height)
        if self._layout != key:
            self._layout = key
            self.layout_messages()

    @objc.python_method
    def layout_messages(self, *, force_bottom=False):
        viewport = self.scroll.contentView().bounds()
        at_bottom = self.document.frame().size.height - viewport.origin.y - viewport.size.height < 36
        width, y = viewport.size.width, 8
        reduce = AK.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
        for bubble in self.messages:
            bw, bh = bubble.measure(max(80, width * 0.78) if bubble.role == "user" else max(80, width))
            x = width - bw if bubble.role == "user" else 0
            target = (x, y, bw, bh)
            if bubble.motion is None:
                bubble.motion = Spring(target)
            else:
                bubble.motion.move_to(target, immediate=reduce)
            bubble.setFrame_(NSMakeRect(*bubble.motion.value))
            bubble.layout_contents(bubble.motion.value[2], bubble.motion.value[3])
            y += bh + 16
        self.document.setFrame_(NSMakeRect(0, 0, width, max(viewport.size.height, y)))
        if at_bottom or force_bottom:
            destination = (0, max(0, y - viewport.size.height))
            if force_bottom or reduce:
                self.document.scrollPoint_(destination)
            elif abs(destination[1] - viewport.origin.y) > 0.1:
                AK.NSAnimationContext.beginGrouping()
                AK.NSAnimationContext.currentContext().setDuration_(0.16)
                self.scroll.contentView().animator().setBoundsOrigin_(destination)
                AK.NSAnimationContext.endGrouping()
            self.scroll.reflectScrolledClipView_(self.scroll.contentView())

    def textDidChange_(self, notification):
        self._refresh_send()
        self.dirty = True
        self.edited_at = time.monotonic()

    @objc.python_method
    def _refresh_send(self):
        typed = bool(self.prompt.string().strip()) or bool(self.files)
        ready = typed and self.pending is None
        self.placeholder.setHidden_(bool(self.prompt.string()))
        self.send_button.setEnabled_(ready)
        set_key_live(self.send_button, ready)
        self.rule.set_live(typed)

    def toggleThink_(self, sender):
        try:
            self.store.set_brain(not self.store.brain)
        except (sqlite3.Error, OSError):
            self.set_status("Could not save the reply mode.")
            return
        self._show_mode()

    @objc.python_method
    def _show_mode(self):
        think = self.store.brain
        self.think_button.setImage_(symbol("brain", AK.NSColor.whiteColor() if think else white(0.64), 14))
        self.think_button.layer().setBackgroundColor_(white(0.16 if think else 0).CGColor())
        tip = "Think is on: Luna takes a moment to reason" if think else "Think: take a moment to reason first"
        self.think_button.setToolTip_(tip)
        self.think_button.setAccessibilityValue_("on" if think else "off")

    def chooseFiles_(self, sender):
        dialog = AK.NSOpenPanel.openPanel()
        dialog.setCanChooseFiles_(True)
        dialog.setCanChooseDirectories_(False)
        dialog.setAllowsMultipleSelection_(True)
        dialog.setMessage_("Choose files to attach to your next message")
        AK.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if dialog.runModal() == AK.NSModalResponseOK:
            self.attach_paths([url.path() for url in dialog.URLs()])

    def mouseEntered_(self, event):
        enter_cursor(self, AK.NSCursor.IBeamCursor())

    def mouseExited_(self, event):
        exit_cursor(self)

    def textDidBeginEditing_(self, notification):
        self.rule.set_live(True)

    def textDidEndEditing_(self, notification):
        self._refresh_send()

    @objc.python_method
    def append(self, role, text, files=(), message_id=None, steps=(), error=None):
        bubble = Bubble.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
        bubble.build(role, text, files, steps, error)
        bubble.owner = self
        bubble.message_id = message_id
        bubble.setWantsLayer_(True)
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
        messages = self.store.messages()
        for message in messages:
            if message["role"] == "luna":
                failed = message["status"] == "failed"
                self.append("luna", reply_text(message["text"], message["status"]), (), message["id"],
                            message["steps"], GENERIC_FAILURE if failed else None)
            else:
                self.append("user", message["text"], message["files"], message["id"])
        last = next((m for m in reversed(messages) if m["role"] == "luna"), None)
        self.attention = bool(last and (last["status"] == "failed"))
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
        self.attachments_document.setFrame_(NSMakeRect(0, 0, max(1, x), 28))
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
        self.status = (f"Couldn't attach {rejected} file{'s' if rejected != 1 else ''}; "
                       f"{'they are' if rejected != 1 else 'it is'} missing or unreadable.") if rejected else ""
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

    @objc.python_method
    def _history(self, messages):
        # Failed replies are not sent back to the model as Luna's words.
        return tuple({"role": m["role"], "text": m["text"], "files": tuple(m["files"])} for m in messages
                     if m["role"] == "user" or (m["status"] == "complete" and m["text"]))

    def send_(self, sender):
        text = self.prompt.string().strip()
        if (not text and not self.files) or self.pending:
            return
        if self.files and any(not Path(path).is_file() or not os.access(path, os.R_OK) for path in self.files):
            self.set_status("A file is no longer available. Remove it or drop it again.")
            return
        history = self._history(self.store.messages())
        try:
            user_id, reply_id = self.store.send(text, self.files)
        except (sqlite3.Error, OSError):
            self.set_status("Could not save this message. Please try again.")
            return
        self.append("user", text, self.files, user_id)
        self.append("luna", "…", message_id=reply_id)
        self.streams[self.store.active] = reply_id
        self.submit(TextRequest(self.store.active, reply_id, history, text, tuple(self.files), self.store.brain))
        self.prompt.setString_("")
        self.files = []
        self.status = ""
        self.attention = False
        self._rebuild_attachments()
        self.textDidChange_(None)
        self.dirty = False
        self.owner.full_chat = True
        self.owner.open_chat()
        self.owner._draw(animate=True)
        self.layout_messages(force_bottom=True)
        self.owner.panel.makeFirstResponder_(self.prompt)

    @objc.python_method
    def retry(self, bubble):
        """Ask again for a failed reply, in place: the same user turn, without duplicating it."""
        if self.pending is not None:
            return
        try:
            user, before = self.store.retry(bubble.message_id)
        except (sqlite3.Error, OSError, StopIteration):
            self.set_status("Could not retry this reply.")
            return
        bubble.update("…", (), None)
        self.streams[self.store.active] = bubble.message_id
        self.attention = False
        self.submit(TextRequest(self.store.active, bubble.message_id, self._history(before), user["text"],
                                tuple(user["files"]), self.store.brain))
        self._refresh_send()
        self.layout_messages(force_bottom=True)

    @objc.python_method
    def tick(self, now):
        elapsed = max(0, now - self.message_time)
        self.message_time = now
        if self.dirty and now - self.edited_at >= 0.3:
            self.flush_draft()
        for bubble in self.messages:
            if bubble.motion and bubble.motion.moving:
                x, y, width, height = bubble.motion.advance(elapsed)
                bubble.setFrame_(NSMakeRect(x, y, width, height))
                bubble.layout_contents(width, height)

    @objc.python_method
    def receive(self, event):
        """Apply a TextEvent from the agent thread; replies to inactive chats only update storage."""
        final = event.status != "pending"
        try:
            self.store.update_reply(event.message_id, event.text, status=event.status, model=event.model,
                                    steps=event.steps)
        except (sqlite3.Error, OSError):
            if final:
                self.set_status("Could not save Luna's reply.")
        if final and self.streams.get(event.chat_id) == event.message_id:
            self.streams.pop(event.chat_id)
        if event.chat_id != self.store.active:
            return
        bubble = self._bubble(event.message_id)
        failed = event.status == "failed"
        if bubble is not None:
            bubble.update(reply_text(event.text, event.status), event.steps,
                          (event.error or GENERIC_FAILURE) if failed else None)
            self.layout_messages()
        if final:
            self.attention = failed
            self._refresh_send()

    @objc.python_method
    def close(self):
        saved = self.flush_draft(force=True)
        self.store.close()
        if not saved:
            raise RuntimeError("Luna could not save the text-chat draft.")
