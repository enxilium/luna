"""A system-wide shortcut through Carbon's RegisterEventHotKey: works from any app, needs no Accessibility access."""

import ctypes
from ctypes import CFUNCTYPE, POINTER, Structure, byref, c_int32, c_uint32, c_void_p

from loguru import logger

# Choices offered in Settings. Values are (virtual key code, Carbon modifier mask, label).
CMD, SHIFT, OPTION, CONTROL = 1 << 8, 1 << 9, 1 << 11, 1 << 12
SHORTCUTS = {
    "ctrl+option+space": (49, CONTROL | OPTION, "⌃⌥Space"),
    "option+space": (49, OPTION, "⌥Space"),
    "cmd+shift+space": (49, CMD | SHIFT, "⇧⌘Space"),
    "ctrl+option+l": (37, CONTROL | OPTION, "⌃⌥L"),
    "cmd+shift+l": (37, CMD | SHIFT, "⇧⌘L"),
}
DEFAULT_SHORTCUT = "ctrl+option+space"


def _fourcc(text):
    return int.from_bytes(text.encode(), "big")


class _HotKeyID(Structure):
    _fields_ = [("signature", c_uint32), ("id", c_uint32)]


class _EventType(Structure):
    _fields_ = [("eventClass", c_uint32), ("eventKind", c_uint32)]


_HANDLER = CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)
_carbon = None


def _load():
    global _carbon
    if _carbon is None:
        lib = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        lib.GetApplicationEventTarget.restype = c_void_p
        lib.InstallEventHandler.argtypes = [c_void_p, _HANDLER, c_uint32, POINTER(_EventType), c_void_p, POINTER(c_void_p)]
        lib.InstallEventHandler.restype = c_int32
        lib.RegisterEventHotKey.argtypes = [c_uint32, c_uint32, _HotKeyID, c_void_p, c_uint32, POINTER(c_void_p)]
        lib.RegisterEventHotKey.restype = c_int32
        lib.UnregisterEventHotKey.argtypes = [c_void_p]
        lib.UnregisterEventHotKey.restype = c_int32
        _carbon = lib
    return _carbon


class HotKey:
    """One registered shortcut. `callback` runs on the main thread (Carbon delivers through the main run loop)."""

    def __init__(self, callback):
        self.callback = callback
        self.ref = None
        self.shortcut = None
        carbon = _load()
        self._handler = _HANDLER(self._fired)  # kept alive for as long as Carbon may call it
        spec = _EventType(_fourcc("keyb"), 5)  # kEventClassKeyboard, kEventHotKeyPressed
        status = carbon.InstallEventHandler(carbon.GetApplicationEventTarget(), self._handler, 1, byref(spec), None, None)
        if status:
            raise OSError(f"InstallEventHandler failed ({status})")

    def _fired(self, call, event, data):
        try:
            self.callback()
        except Exception:
            logger.exception("Dashboard shortcut handler failed")
        return 0

    def set(self, shortcut):
        """Register `shortcut` (a SHORTCUTS key) in place of the current one. Returns False if macOS refused it."""
        if shortcut not in SHORTCUTS:
            shortcut = DEFAULT_SHORTCUT
        if shortcut == self.shortcut and self.ref is not None:
            return True
        self.clear()
        code, modifiers, _ = SHORTCUTS[shortcut]
        ref = c_void_p()
        carbon = _load()
        status = carbon.RegisterEventHotKey(code, modifiers, _HotKeyID(_fourcc("LUNA"), 1),
                                            carbon.GetApplicationEventTarget(), 0, byref(ref))
        if status:
            logger.warning("Dashboard shortcut {} unavailable ({})", shortcut, status)
            return False
        self.ref, self.shortcut = ref, shortcut
        return True

    def clear(self):
        if self.ref is not None:
            _load().UnregisterEventHotKey(self.ref)
        self.ref = self.shortcut = None
