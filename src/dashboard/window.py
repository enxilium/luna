"""The dashboard overlay: a frosted glass window that a global shortcut shows and hides from anywhere.

The whole window is one behind-window blur (the notch's material) with a tint on top; the transparent web view draws
the panels and content over it. It floats above other apps, follows you across Spaces, and hides when you click away.
"""

import asyncio
from contextlib import contextmanager
import fcntl
import math
import os
import signal
import threading

import AppKit as AK
from Foundation import NSObject, NSURL, NSURLRequest, NSMakeRect
import objc
from PyObjCTools import AppHelper
import Quartz  # noqa: F401  (gives PyObjC the CGColor type, so tint layers get real colors)
import WebKit as WK

from src.dashboard.hotkey import DEFAULT_SHORTCUT, HotKey
from src.dashboard.location import open_location
from src.dashboard.server import Dashboard, build, serve
from src.dashboard.canvas import Quercus
from src.dashboard.store import DASHBOARD_PATH

FADE = 0.16
LIGHTS = (20, 20)  # traffic lights: the close button's x and the step to the next, centred on the top bar


def _color(white, alpha):
    return AK.NSColor.colorWithWhite_alpha_(white, max(0.0, min(1.0, alpha))).CGColor()


class FlippedView(AK.NSView):
    def isFlipped(self):
        return True


class DragStrip(FlippedView):
    """Drags the window from the top bar, except over the controls the page reports as holes."""

    def initWithFrame_(self, frame):
        self = objc.super(DragStrip, self).initWithFrame_(frame)
        if self is not None:
            self.height, self.holes = 0, []
        return self

    def hitTest_(self, point):
        p = self.convertPoint_fromView_(point, self.superview())
        if p.y > self.height or p.y < 0:
            return None
        for x, y, w, h in self.holes:
            if x <= p.x <= x + w and y <= p.y <= y + h:
                return None
        return self

    def mouseDownCanMoveWindow(self):
        return True

    def mouseDown_(self, event):
        if event.clickCount() == 2:
            self.window().performZoom_(None)
        else:
            self.window().performWindowDragWithEvent_(event)


class _Bridge(NSObject):
    """Messages from the page: top-bar holes, glass style, shortcut."""

    @objc.python_method
    def configure(self, app):
        self.app = app
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        body = message.body()
        try:
            kind = body["type"]
        except (KeyError, TypeError):
            return
        if kind == "chrome":
            self.app.drag.height = float(body["height"])
            self.app.place_lights()
            self.app.drag.holes = [tuple(float(v) for v in hole) for hole in body["holes"]]
        elif kind == "glass":
            self.app.set_glass(str(body.get("style") or "frosted"), float(body.get("tint") or 0.32))
        elif kind == "shortcut":
            self.app.set_shortcut(str(body.get("value") or DEFAULT_SHORTCUT))


class _Navigation(NSObject):
    """Keep the web view on Luna's own origin; anything else opens in the default browser."""

    @objc.python_method
    def configure(self, origin):
        self.origin = origin
        return self

    def webView_decidePolicyForNavigationAction_decisionHandler_(self, view, action, handler):
        url = action.request().URL()
        text = str(url.absoluteString()) if url is not None else ""
        if text.startswith(self.origin) or text.startswith("about:"):
            handler(WK.WKNavigationActionPolicyAllow)
            return
        if text.startswith(("https://", "http://")):
            AK.NSWorkspace.sharedWorkspace().openURL_(url)
        handler(WK.WKNavigationActionPolicyCancel)

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(self, view, config, action, features):
        url = action.request().URL()
        if url is not None and str(url.scheme()) in ("http", "https"):
            AK.NSWorkspace.sharedWorkspace().openURL_(url)
        return None


class DashboardApp(NSObject):
    @objc.python_method
    def build(self, *, demo=False, show=True):
        self.demo = demo
        self.loop = self.stop = self.failure = None
        self.style = {"style": "frosted", "tint": 0.32}
        self.hiding = False
        app = AK.NSApplication.sharedApplication()
        # An ordinary app: a Dock icon and an app switcher entry, so the window stays put when a permission prompt or
        # another app takes focus. The main menu provides ⌘C/⌘V/⌘W/⌘Q.
        app.setActivationPolicy_(AK.NSApplicationActivationPolicyRegular)
        app.setApplicationIconImage_(dock_icon())
        app.setMainMenu_(main_menu())

        style = (AK.NSWindowStyleMaskTitled | AK.NSWindowStyleMaskClosable | AK.NSWindowStyleMaskMiniaturizable |
                 AK.NSWindowStyleMaskResizable | AK.NSWindowStyleMaskFullSizeContentView)
        screen = AK.NSScreen.mainScreen().visibleFrame()
        width, height = min(1380, screen.size.width - 80), min(900, screen.size.height - 60)
        self.window = AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height), style, AK.NSBackingStoreBuffered, False)
        self.window.setTitle_("Luna")
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(AK.NSWindowTitleHidden)
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(AK.NSColor.clearColor())
        self.window.setCollectionBehavior_(AK.NSWindowCollectionBehaviorFullScreenPrimary)
        self.window.setHidesOnDeactivate_(False)
        self.window.setMinSize_((900, 620))
        self.window.setAppearance_(AK.NSAppearance.appearanceNamed_(AK.NSAppearanceNameDarkAqua))
        self.window.setReleasedWhenClosed_(False)
        self.window.center()
        self.window.setFrameAutosaveName_("LunaDashboard")
        self.window.setDelegate_(self)

        sizing = AK.NSViewWidthSizable | AK.NSViewHeightSizable
        # The same material as the notch's glass, so the dashboard reads as the island grown into a window.
        root = AK.NSView.alloc().initWithFrame_(self.window.contentView().bounds())
        self.window.setContentView_(root)
        self.effect = AK.NSVisualEffectView.alloc().initWithFrame_(root.bounds())
        self.effect.setMaterial_(AK.NSVisualEffectMaterialHUDWindow)
        self.effect.setBlendingMode_(AK.NSVisualEffectBlendingModeBehindWindow)
        self.effect.setState_(AK.NSVisualEffectStateActive)
        self.effect.setAppearance_(AK.NSAppearance.appearanceNamed_(AK.NSAppearanceNameVibrantDark))
        self.effect.setAutoresizingMask_(sizing)
        root.addSubview_(self.effect)
        self.tint = AK.NSView.alloc().initWithFrame_(root.bounds())
        self.tint.setWantsLayer_(True)
        self.tint.setAutoresizingMask_(sizing)
        root.addSubview_(self.tint)
        self.set_glass("frosted", 0.32)

        config = WK.WKWebViewConfiguration.alloc().init()
        self.bridge = _Bridge.alloc().init().configure(self)
        config.userContentController().addScriptMessageHandler_name_(self.bridge, "luna")
        config.preferences().setValue_forKey_(True, "developerExtrasEnabled")
        self.web = WK.WKWebView.alloc().initWithFrame_configuration_(root.bounds(), config)
        self.web.setAutoresizingMask_(sizing)
        self.web.setValue_forKey_(False, "drawsBackground")
        self.web.setUnderPageBackgroundColor_(AK.NSColor.clearColor())
        self.web.setAllowsMagnification_(False)
        if hasattr(self.web, "setInspectable_"):
            self.web.setInspectable_(True)
        root.addSubview_(self.web)
        self.drag = DragStrip.alloc().initWithFrame_(root.bounds())
        self.drag.setAutoresizingMask_(sizing)
        root.addSubview_(self.drag)
        self.hotkey = HotKey(self.show)
        self.hotkey.set(DEFAULT_SHORTCUT)
        self.place_lights()
        if show:
            self.show()

    # Window -----------------------------------------------------------------------------------------------------

    @objc.python_method
    def visible(self):
        return self.window.isVisible() and not self.hiding

    @objc.python_method
    def show(self):
        """The shortcut and the Dock icon bring the window to the front (un-minimizing it)."""
        self.hiding = False
        if self.window.isMiniaturized():
            self.window.deminiaturize_(None)
        if not self.window.isVisible():
            self.window.setAlphaValue_(0.0)
        self.window.makeKeyAndOrderFront_(None)
        AK.NSApp.activateIgnoringOtherApps_(True)
        self._fade(1.0, None)
        self.web.evaluateJavaScript_completionHandler_("window.dispatchEvent(new Event('luna-show'))", None)

    @objc.python_method
    def hide(self):
        """⌘W: the window fades out and Luna keeps running; the Dock icon or the shortcut brings it back."""
        if not self.window.isVisible() or self.hiding or self.window.attachedSheet() is not None:
            return
        self.hiding = True

        def done():
            if self.hiding:
                self.window.orderOut_(None)
                self.hiding = False

        self._fade(0.0, done)

    @objc.python_method
    def _fade(self, alpha, done):
        def group(context):
            context.setDuration_(FADE)
            self.window.animator().setAlphaValue_(alpha)

        def finished():  # PyObjC aborts the app if a void block returns a value
            if done is not None:
                done()

        AK.NSAnimationContext.runAnimationGroup_completionHandler_(group, finished)

    @objc.python_method
    def set_shortcut(self, shortcut):
        ok = self.hotkey.set(shortcut)
        self.web.evaluateJavaScript_completionHandler_(
            f"window.dispatchEvent(new CustomEvent('luna-shortcut', {{ detail: {'true' if ok else 'false'} }}))", None)

    @objc.python_method
    def set_glass(self, style, tint):
        style = style if style in ("clear", "frosted", "dark") else "frosted"
        tint = max(0.0, min(0.8, tint))
        self.style = {"style": style, "tint": tint}
        # Clear lets more of the desktop through; Dark deepens the tint.
        self.effect.setAlphaValue_(0.75 if style == "clear" else 1.0)
        alpha = tint * (0.5 if style == "clear" else 1) + (0.3 if style == "dark" else 0)
        self.tint.layer().setBackgroundColor_(_color(0, alpha))

    # Server -----------------------------------------------------------------------------------------------------

    @objc.python_method
    def start_server(self):
        store, hub = build(demo=self.demo)
        dashboard = Dashboard(store, hub, demo=self.demo, location=open_location(self.demo),  # main thread: CoreLocation's run loop
                              quercus=None if self.demo else Quercus(store))

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop, self.stop = loop, asyncio.Event()
            try:
                loop.run_until_complete(serve(dashboard, ready=lambda url: AppHelper.callAfter(self.open, url), stop=self.stop))
            except Exception as error:
                self.failure = error
                AppHelper.callAfter(AK.NSApp.terminate_, None)
            finally:
                store.close()
                loop.close()

        self.thread = threading.Thread(target=run, name="luna-dashboard", daemon=True)
        self.thread.start()

    @objc.python_method
    def open(self, url):
        self.origin = url.split("/?", 1)[0]
        self.navigation = _Navigation.alloc().init().configure(self.origin)
        self.web.setNavigationDelegate_(self.navigation)
        self.web.setUIDelegate_(self.navigation)
        self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))

    @objc.python_method
    def shutdown(self):
        if self.loop is not None and self.stop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.stop.set)
        if getattr(self, "thread", None) is not None:
            self.thread.join(timeout=5)

    def applicationWillTerminate_(self, notification):
        # terminate: ends the process without unwinding the run loop, so the server stops here.
        self.shutdown()

    @objc.python_method
    def place_lights(self):
        """Centre the close, minimize and zoom buttons on the page's top bar (the page leaves room for them). AppKit lays
        them out for a 28 pt title bar, so this runs again whenever it may have: resizing, leaving full screen."""
        if getattr(self, "drag", None) is None or self.window.styleMask() & AK.NSWindowStyleMaskFullScreen:
            return
        buttons = [self.window.standardWindowButton_(kind)
                   for kind in (AK.NSWindowCloseButton, AK.NSWindowMiniaturizeButton, AK.NSWindowZoomButton)]
        container = buttons[0].superview().superview()  # NSTitlebarContainerView
        if container is None:
            return
        height = self.drag.height or 54.0  # the bar's height, once the page has reported it
        frame = container.frame()
        container.setFrame_(NSMakeRect(frame.origin.x, self.window.frame().size.height - height, frame.size.width, height))
        x, step = LIGHTS
        for index, button in enumerate(buttons):
            size = button.frame().size
            button.setFrameOrigin_((x + index * step, round((height - size.height) / 2)))

    def windowDidResize_(self, notification):
        self.place_lights()

    def windowDidExitFullScreen_(self, notification):
        self.place_lights()

    def windowShouldClose_(self, sender):
        self.hide()
        return False

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
        self.show()
        return True


def main_menu():
    def item(title, action, key="", mask=AK.NSEventModifierFlagCommand):
        entry = AK.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        entry.setKeyEquivalentModifierMask_(mask)
        return entry

    def menu(title, *entries):
        holder = AK.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        submenu = AK.NSMenu.alloc().initWithTitle_(title)
        for entry in entries:
            submenu.addItem_(entry or AK.NSMenuItem.separatorItem())
        holder.setSubmenu_(submenu)
        return holder

    bar = AK.NSMenu.alloc().init()
    bar.addItem_(menu("Luna", item("About Luna", "orderFrontStandardAboutPanel:"), None,
                      item("Hide Luna", "hide:", "h"),
                      item("Hide Others", "hideOtherApplications:", "h", AK.NSEventModifierFlagCommand | AK.NSEventModifierFlagOption),
                      item("Show All", "unhideAllApplications:"), None, item("Quit Luna", "terminate:", "q")))
    # WKWebView only gets copy, paste and undo through these standard menu actions.
    bar.addItem_(menu("Edit", item("Undo", "undo:", "z"),
                      item("Redo", "redo:", "z", AK.NSEventModifierFlagCommand | AK.NSEventModifierFlagShift), None,
                      item("Cut", "cut:", "x"), item("Copy", "copy:", "c"), item("Paste", "paste:", "v"),
                      item("Select All", "selectAll:", "a")))
    bar.addItem_(menu("View", item("Reload", "reload:", "r"),
                      item("Enter Full Screen", "toggleFullScreen:", "f", AK.NSEventModifierFlagCommand | AK.NSEventModifierFlagControl)))
    bar.addItem_(menu("Window", item("Minimize", "performMiniaturize:", "m"), item("Zoom", "performZoom:"),
                      item("Close", "performClose:", "w")))
    return bar


def dock_icon(size=512):
    """The Diamond Ring on night glass, drawn at launch so there's no asset to ship."""
    image = AK.NSImage.alloc().initWithSize_((size, size))
    image.lockFocus()
    try:
        inset = size * 0.09
        rect = NSMakeRect(inset, inset, size - 2 * inset, size - 2 * inset)
        body = AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, size * 0.2, size * 0.2)
        AK.NSGradient.alloc().initWithStartingColor_endingColor_(
            AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.12, 0.11, 0.22, 1),
            AK.NSColor.colorWithSRGBRed_green_blue_alpha_(0.03, 0.03, 0.06, 1)).drawInBezierPath_angle_(body, -90)
        AK.NSColor.colorWithWhite_alpha_(1, 0.16).setStroke()
        body.setLineWidth_(size * 0.006)
        body.stroke()
        center, radius = size / 2, size * 0.2
        for width, alpha, color in ((size * 0.08, 0.18, (0.49, 0.55, 1)), (size * 0.045, 0.35, (0.66, 0.45, 1)),
                                    (size * 0.022, 0.95, (0.92, 0.9, 1))):
            AK.NSColor.colorWithSRGBRed_green_blue_alpha_(*color, alpha).setStroke()
            ring = AK.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(center - radius, center - radius, 2 * radius, 2 * radius))
            ring.setLineWidth_(width)
            ring.stroke()
        angle = math.radians(45)
        x, y = center + radius * math.cos(angle), center + radius * math.sin(angle)
        for r, alpha in ((size * 0.07, 0.25), (size * 0.04, 0.6), (size * 0.022, 1)):
            AK.NSColor.colorWithWhite_alpha_(1, alpha).setFill()
            AK.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x - r, y - r, 2 * r, 2 * r)).fill()
    finally:
        image.unlockFocus()
    return image


@contextmanager
def single_instance(*, demo=False):
    """One dashboard over your data at a time (demo data is in memory, so demos don't count)."""
    lock_path = DASHBOARD_PATH.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600), "a+") as lock:
        if not demo:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Luna's dashboard is already open.") from None
        yield


def run_dashboard(*, demo=False):
    """The dashboard on its own (development); `src.ui.app.run_app` runs it inside Luna with the notch and voice."""
    from src import setup

    setup.DOTENV = False
    with single_instance(demo=demo):
        controller = DashboardApp.alloc().init()
        controller.build(demo=demo)
        app = AK.NSApplication.sharedApplication()
        app.setDelegate_(controller)
        controller.start_server()
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        for sig in previous:
            signal.signal(sig, lambda *_: AppHelper.callAfter(AK.NSApp.terminate_, None))
        # Python runs signal handlers only when it gets control; a hidden overlay's run loop may idle for hours.
        heartbeat = AK.NSTimer.timerWithTimeInterval_repeats_block_(0.5, True, lambda timer: None)
        AK.NSRunLoop.mainRunLoop().addTimer_forMode_(heartbeat, AK.NSRunLoopCommonModes)
        try:
            AppHelper.runEventLoop(installInterrupt=False)
        finally:
            heartbeat.invalidate()
            controller.shutdown()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        if controller.failure:
            raise controller.failure
