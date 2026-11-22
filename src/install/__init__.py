"""Luna's installers: memory, Google, Spotify and Canvas, run from setup in the app (or the scripts/setup_* commands).

Each is a `Job`: one run at a time in a thread, with a step name, progress and, when it fails, a reason code (never a
provider's own text). A successful one bumps `generation`, which the voice side watches to restart with the new
integrations."""

import threading
import time

from loguru import logger

_generation = [0]


def generation():
    """How many installers have finished since Luna started; a change means integrations or memory changed."""
    return _generation[0]


def changed():
    _generation[0] += 1


class InstallError(Exception):
    """A failure with a reason code the setup page explains (`disk_full`, `download_failed`, `token_rejected`…)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Job:
    def __init__(self, name, run, ready):
        self.name, self._run, self.ready = name, run, ready
        self.state, self.step, self.done, self.total, self.error = "idle", None, 0, 0, None
        self.lock = threading.Lock()
        self.on_change = None
        self._last = 0.0

    def snapshot(self):
        state = "done" if self.state != "running" and self.ready() else self.state
        return {"state": state, "step": self.step, "done": self.done, "total": self.total, "error": self.error}

    def start(self, **options):
        with self.lock:
            if self.state == "running":
                return False
            self.state, self.step, self.done, self.total, self.error = "running", None, 0, 0, None
        threading.Thread(target=self._thread, kwargs=options, name=f"luna-install-{self.name}", daemon=True).start()
        self._changed(force=True)
        return True

    def report(self, step=None, done=None, total=None):
        """Called by the installer: a new step resets progress; progress alone updates it (throttled for the page)."""
        if step is not None and step != self.step:
            self.step, self.done, self.total = step, 0, 0
            self._changed(force=True)
        if done is not None:
            self.done, self.total = done, total or self.total
            self._changed()

    def _changed(self, force=False):
        if self.on_change and (force or time.monotonic() - self._last > 0.2):
            self._last = time.monotonic()
            self.on_change(self.name, self.snapshot())

    def _thread(self, **options):
        try:
            self._run(self, **options)
            self.state = "done"
            changed()
        except InstallError as error:
            logger.warning("Setting up {} stopped: {}", self.name, error.reason)
            self.state, self.error = "failed", error.reason
        except Exception as error:  # noqa: BLE001  (logged by type only; the page gets a code)
            from src.integrations.auth import AuthFailure, AuthRequired

            # Keychain trouble isn't a connection problem; its codes are safe to show (never provider text).
            keychain = isinstance(error, (AuthRequired, AuthFailure)) and str(error).startswith("keychain_")
            logger.warning("Setting up {} failed ({}).", self.name, str(error) if keychain else type(error).__name__)
            self.state, self.error = "failed", str(error) if keychain else "install_failed"
        self._changed(force=True)
