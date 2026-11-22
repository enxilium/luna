"""What Luna needs before it can listen, and the pieces of the first-run setup that provide it.

Voice needs a Gemini API key and the wake word model; everything else (microphone, calendar, location, Messages) is a
permission Luna asks for or works without. The key lives in the Keychain (service "Luna"); a profile in a separate
LUNA_HOME keeps its own key and ignores GOOGLE_API_KEY and the checkout's .env, so trying setup as a new user shares
nothing with your own Luna.
"""

import hashlib
import os
from pathlib import Path
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from loguru import logger

from src.audio.wake_word import download_model, model_ready
from src.paths import HOME, keychain_service, separate_home

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
KEYCHAIN_SERVICE = keychain_service("Luna")
# Scripts and live tests may take the key from the checkout's .env; the app never does (run_app and run_dashboard turn
# this off), so a dev build asks for its key like a new user's Luna and keeps it in its own Keychain entry.
DOTENV = True
KEY_CHECK_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"


# Gemini API key ---------------------------------------------------------------------------------------------------

def _account():
    if not separate_home():
        return "gemini-api-key"
    return "gemini-api-key:" + hashlib.sha256(str(HOME.resolve()).encode()).hexdigest()[:16]


def _query(account):
    import Security as S
    return {S.kSecClass: S.kSecClassGenericPassword, S.kSecAttrService: KEYCHAIN_SERVICE, S.kSecAttrAccount: account}


def _keychain_read(account):
    import Security as S
    status, data = S.SecItemCopyMatching(_query(account) | {S.kSecReturnData: True, S.kSecMatchLimit: S.kSecMatchLimitOne}, None)
    return bytes(data).decode() if status == S.errSecSuccess else None


def _keychain_write(account, value):
    import Security as S
    from Foundation import NSData
    payload = value.encode()
    data = NSData.dataWithBytes_length_(payload, len(payload))
    status = S.SecItemUpdate(_query(account), {S.kSecValueData: data})
    if status == S.errSecItemNotFound:
        status, _ = S.SecItemAdd(_query(account) | {S.kSecValueData: data}, None)
    if status != S.errSecSuccess:
        raise RuntimeError("keychain_unavailable")


def gemini_key():
    """The key from GOOGLE_API_KEY, the Keychain, or (scripts only) the checkout's .env, in that order, or ""."""
    if not separate_home():
        key = os.environ.get("GOOGLE_API_KEY", "").strip()
        if key:
            return key
    key = (_keychain_read(_account()) or "").strip()
    if key or separate_home() or not DOTENV:
        return key
    from dotenv import dotenv_values
    return (dotenv_values(ENV_PATH).get("GOOGLE_API_KEY") or "").strip() if ENV_PATH.exists() else ""


def check_gemini_key(key):
    """"ok", "invalid_key" or "unreachable". The key goes to Google in a header, never in the URL."""
    try:
        with urlopen(Request(KEY_CHECK_URL, headers={"x-goog-api-key": key}), timeout=15):
            return "ok"
    except HTTPError as error:
        return "invalid_key" if error.code in (400, 401, 403) else "unreachable"
    except (URLError, TimeoutError, OSError):
        return "unreachable"


def save_gemini_key(key):
    """Check the key with Google and keep it in the Keychain. Returns check_gemini_key's answer."""
    key = key.strip()
    if not key:
        return "invalid_key"
    result = check_gemini_key(key)
    if result == "ok":
        _keychain_write(_account(), key)
    return result


# Microphone ---------------------------------------------------------------------------------------------------------

MIC_STATES = {0: "undetermined", 1: "restricted", 2: "denied", 3: "granted"}


def microphone():
    import AVFoundation as AV
    return MIC_STATES.get(AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio), "undetermined")


def request_microphone(done):
    """Ask macOS once (it only shows the prompt while undetermined); `done(state)` follows on any thread."""
    import AVFoundation as AV

    def answered(granted):  # PyObjC aborts the app if a void block returns a value
        done(microphone())

    AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AV.AVMediaTypeAudio, answered)


# Wake word model ----------------------------------------------------------------------------------------------------

class Download:
    """The wake word model's one download, in a thread; `on_change` hears progress (throttled) and the end."""

    def __init__(self):
        self.state, self.done, self.total, self.error = ("done" if model_ready() else "idle"), 0, 0, None
        self.lock = threading.Lock()
        self.on_change = None

    def snapshot(self):
        return {"state": self.state, "done": self.done, "total": self.total, "error": self.error}

    def start(self):
        with self.lock:
            if self.state in ("running", "done") or model_ready():
                self.state = "done" if model_ready() else self.state
                return
            self.state, self.done, self.total, self.error = "running", 0, 0, None
        threading.Thread(target=self._run, name="luna-wake-model", daemon=True).start()

    def _changed(self):
        if self.on_change:
            self.on_change(self.snapshot())

    def _run(self):
        last = [0.0]

        def progress(done, total):
            import time
            self.done, self.total = done, total
            if time.monotonic() - last[0] > 0.15:
                last[0] = time.monotonic()
                self._changed()

        try:
            download_model(progress)
            self.state = "done"
        except Exception as error:  # noqa: BLE001  (shown as a reason code, logged here)
            logger.warning("Wake word model download failed ({}).", type(error).__name__)
            self.state, self.error = "failed", "download_failed"
        self._changed()


download = Download()


# Connections: memory, Google, Spotify, Canvas (src/install) -------------------------------------------------------------

def _jobs():
    from src import resources
    from src.install import Job, canvas, google, memory, spotify

    # What this copy of Luna can offer: a packaged app carries its tools and the OAuth apps it was built with.
    available = {"memory": lambda: bool(resources.uv() and resources.ollama()),
                 "google": lambda: resources.google_client() is not None,
                 "spotify": lambda: resources.spotify_app() is not None,
                 "canvas": lambda: bool(resources.uv())}
    jobs = {name: Job(name, module.install, module.ready)
            for name, module in (("memory", memory), ("google", google), ("spotify", spotify), ("canvas", canvas))}
    return jobs, available


JOBS, AVAILABLE = _jobs()


def connections():
    return {name: job.snapshot() | {"available": AVAILABLE[name]()} for name, job in JOBS.items()}


# Overall ------------------------------------------------------------------------------------------------------------

def voice_ready():
    """Whether the voice pipeline (and the notch's chat) can start: a key and the wake word model."""
    return bool(gemini_key()) and model_ready()


def status():
    return {"key": bool(gemini_key()), "microphone": microphone(), "wake_model": download.snapshot(),
            "voice_ready": voice_ready(), "separate_home": separate_home(), "connections": connections()}
