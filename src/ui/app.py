"""Luna as one app: the notch, the dashboard and the voice pipeline in one process, started and stopped together.

AppKit (the notch and the dashboard window) runs on the main thread, the voice pipeline on its asyncio thread, and the
dashboard's server on its own. Quitting from anywhere, or any of them failing, stops all three."""

import asyncio
import fcntl
import os
import queue
import signal
import threading

import AppKit as AK
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSObject, NSTimer
import objc
from loguru import logger

from src.agent.text import TextEvent
from src.ui.notch import NotchWindow
from src.ui.chats import CHAT_PATH, ChatStore


READY = object()  # sent to the chat queue when setup finishes, so the stand-in replier hands over to the real one


async def _stand_in_replies(requests, publish, error):
    while (request := await asyncio.to_thread(requests.get)) not in (None, READY):
        publish(TextEvent(request.chat_id, request.message_id, "", "failed", error=error))


async def _wait_for_setup(stopped, requests, publish):
    """Until Luna has a Gemini key and the wake word model, voice waits (the dashboard shows setup) and chats explain."""
    from src.setup import voice_ready

    if await asyncio.to_thread(voice_ready):
        return
    logger.info("Luna isn't set up yet; voice starts when setup is done.")
    replying = asyncio.create_task(_stand_in_replies(
        requests, publish, "Luna isn't set up yet. Press ⌃⌥Space to finish setting up.")) if requests is not None else None
    try:
        while not stopped.is_set() and not await asyncio.to_thread(voice_ready):
            try:
                await asyncio.wait_for(stopped.wait(), 1)
            except TimeoutError:
                pass
    finally:
        if replying:
            requests.put(READY)
            await replying


async def _run_voice(stopped, requests, events):
    """Voice and chat, restarted whenever setup finishes connecting something (memory, Google, Spotify, Canvas): the
    integrations and memory are opened once per run."""
    from src.agent.conversation import run_conversation
    from src.install import generation

    text = {"text_requests": requests, "on_text": events.put} if requests is not None else {}
    while not stopped.is_set():
        seen, restart = generation(), asyncio.Event()

        async def watch():
            while not stopped.is_set() and generation() == seen:
                try:
                    await asyncio.wait_for(stopped.wait(), 1)
                except TimeoutError:
                    pass
            restart.set()

        watching = asyncio.create_task(watch())
        try:
            await run_conversation(stopped=restart, on_activity=events.put, on_audio=events.put, **text)
        finally:
            watching.cancel()
        if not stopped.is_set():
            logger.info("Setup connected something new; restarting voice with it.")


async def _run_agent(commands, events, requests=None, *, preview=False):
    stopped = asyncio.Event()

    async def wait_for_stop():
        await asyncio.to_thread(commands.get)
        stopped.set()

    stopping = asyncio.create_task(wait_for_stop())
    replying = None
    try:
        if preview:
            if requests is not None:
                replying = asyncio.create_task(_stand_in_replies(
                    requests, events.put, "Preview mode doesn't connect to Luna's assistant."))
            await stopped.wait()
        else:
            await _wait_for_setup(stopped, requests, events.put)
            await _run_voice(stopped, requests, events)
    finally:
        commands.put(None)
        if requests is not None:
            requests.put(None)
        await stopping
        if replying:
            await replying


class LunaApp(NSObject):
    @objc.python_method
    def build(self, commands, events, requests, dashboard=None):
        self.commands, self.events, self.dashboard = commands, events, dashboard
        self.notch = NotchWindow.alloc().init()
        self.notch.build(store=ChatStore(), submit=requests.put)
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 60, self, "poll:", None, True,
        )
        NSRunLoop.mainRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

    def poll_(self, timer):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                self.notch.tick()
                return
            if event is None:
                app = AK.NSApplication.sharedApplication()
                app.stop_(None)
                # Wake AppKit's event wait so a timer-driven stop can finish.
                app.postEvent_atStart_(
                    AK.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                        AK.NSEventTypeApplicationDefined, (0, 0), 0, 0, 0, None, 0, 0, 0,
                    ), True,
                )
                return
            if isinstance(event, TextEvent):
                self.notch.chat.receive(event)
            elif isinstance(event, bool):
                self.notch.set_expanded(event)
            else:
                self.notch.set_audio(*event)

    def applicationShouldTerminate_(self, app):
        # ⌘Q anywhere, or the dashboard's server failing: stop the voice pipeline first; its thread ending stops the app.
        self.commands.put(None)
        return AK.NSTerminateCancel

    # The dashboard is this app's window: clicking Luna in the Dock (or opening it again) brings it back.
    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
        if self.dashboard is not None:
            self.dashboard.show()
        return True

    @objc.python_method
    def close(self):
        self.timer.invalidate()
        self.notch.close()


def run_app(*, preview=False, demo=False):
    """Luna: the notch, wake word and voice, and the dashboard (hidden until its shortcut). `preview` is the notch alone,
    with no microphone, Gemini, MCP, memory or dashboard; `demo` gives the dashboard sample data instead of yours."""
    from src import setup

    setup.DOTENV = False
    CHAT_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(CHAT_PATH.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600), "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Luna is already running. Quit it before opening another.") from None
        if preview:
            _run_app(preview=True)
            return
        from src.dashboard.window import single_instance

        with single_instance(demo=demo):
            _run_app(dashboard=True, demo=demo)


def _run_app(*, preview=False, dashboard=False, demo=False):
    commands, events, requests, errors = queue.Queue(), queue.Queue(), queue.Queue(), []
    app = AK.NSApplication.sharedApplication()
    # With the dashboard Luna is an ordinary app (DashboardApp.build sets that); the notch alone stays out of the Dock.
    app.setActivationPolicy_(AK.NSApplicationActivationPolicyAccessory)
    board = None
    if dashboard:
        from src.dashboard.window import DashboardApp

        # Everything starts in the background: the dashboard waits for its shortcut (or for Luna to be opened again).
        from src.setup import voice_ready

        board = DashboardApp.alloc().init()
        board.build(demo=demo, show=not voice_ready())  # first run: open on setup (the page goes there itself)
    controller = LunaApp.alloc().init()
    controller.build(commands, events, requests, dashboard=board)
    app.setDelegate_(controller)
    if board is not None:
        board.start_server()

    def run():
        try:
            asyncio.run(_run_agent(commands, events, requests, preview=preview))
        except Exception as error:
            errors.append(error)
        finally:
            events.put(None)

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in previous:
        signal.signal(sig, lambda *_: commands.put(None))
    background = threading.Thread(target=run, name="luna-agent")
    background.start()
    try:
        app.run()
    finally:
        commands.put(None)
        background.join()
        if board is not None:
            board.shutdown()
        controller.close()
        app.setDelegate_(None)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if errors:
        raise errors[0]
    if board is not None and board.failure:
        raise board.failure
