"""Where this Mac is, from macOS Location Services: town-level, for the weather. Access is asked for only from Settings."""

import threading

from loguru import logger

STATES = {0: "not_determined", 1: "restricted", 2: "denied", 3: "ready", 4: "ready"}  # 3 always, 4 when in use
_delegate = None


def _delegate_class():
    global _delegate
    if _delegate is None:
        from Foundation import NSObject

        class LunaLocationDelegate(NSObject):
            def locationManager_didUpdateLocations_(self, manager, locations):
                if locations:
                    self.owner._update(locations[-1])

            def locationManagerDidChangeAuthorization_(self, manager):
                self.owner._authorization_changed()

            def locationManager_didFailWithError_(self, manager, error):
                logger.info("Location unavailable for now")  # no fix yet, or Wi-Fi off; the last fix stays

        _delegate = LunaLocationDelegate
    return _delegate


class Location:
    """The last town-level fix, kept current by significant-change monitoring.

    CoreLocation reports to the run loop of the thread that made the manager, so it lives on AppKit's main thread
    (`call` defaults to AppHelper.callAfter). Reading never prompts; only `request()` (a click in Settings) may."""

    def __init__(self, call=None):
        import CoreLocation as CL
        from PyObjCTools import AppHelper

        self.CL, self.call = CL, call or AppHelper.callAfter
        self.manager = self.delegate = None
        self.fix = None
        self.on_change = None  # set by the server; called from the main thread
        self.answered = threading.Event()
        self.call(self._start)

    def _start(self):
        manager = self.CL.CLLocationManager.alloc().init()
        self.delegate = _delegate_class().alloc().init()
        self.delegate.owner = self
        manager.setDelegate_(self.delegate)
        manager.setDesiredAccuracy_(self.CL.kCLLocationAccuracyThreeKilometers)
        manager.setDistanceFilter_(3000)
        self.manager = manager
        self._follow()

    def _follow(self):
        if self.status() == "ready":
            self.manager.startMonitoringSignificantLocationChanges()
            self.manager.requestLocation()  # a first fix now; significant changes can take a while to arrive

    def _authorization_changed(self):
        self.answered.set()
        self._follow()
        if self.on_change:
            self.on_change()

    def _update(self, location):
        point = location.coordinate()
        self.fix = (point.latitude, point.longitude)
        if self.on_change:
            self.on_change()

    def status(self):
        if not self.CL.CLLocationManager.locationServicesEnabled():
            return "off"
        manager = self.manager
        value = manager.authorizationStatus() if manager is not None else self.CL.CLLocationManager.authorizationStatus()
        return STATES.get(int(value), "unavailable")

    def request(self, timeout=120):
        """Show macOS's location prompt (when it hasn't been answered) and wait for the answer."""
        if self.status() != "not_determined":
            return self.status()
        self.answered.clear()
        self.call(lambda: self.manager is not None and self.manager.requestWhenInUseAuthorization())
        self.answered.wait(timeout)
        return self.status()

    def coordinate(self):
        if self.status() != "ready":
            return None
        if self.fix is None and self.manager is not None and self.manager.location() is not None:
            point = self.manager.location().coordinate()
            self.fix = (point.latitude, point.longitude)
        return self.fix


class DemoLocation:
    on_change = None

    def status(self):
        return "ready"

    def request(self, timeout=0):
        return "ready"

    def coordinate(self):
        return (43.65, -79.38)


class NoLocation:
    on_change = None

    def status(self):
        return "unavailable"

    def request(self, timeout=0):
        return "unavailable"

    def coordinate(self):
        return None


def open_location(demo=False, main_thread=True):
    """Location for the overlay (AppKit runs the main loop). A browser-only server has no run loop for CoreLocation."""
    if demo:
        return DemoLocation()
    if not main_thread:
        return NoLocation()
    try:
        return Location()
    except Exception:
        logger.warning("Location unavailable; Home shows no weather")
        return NoLocation()
