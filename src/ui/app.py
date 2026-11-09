"""Keep AppKit on the main thread and the voice pipeline on its asyncio thread."""

import asyncio
import fcntl
import os
import queue
import signal
import threading

import AppKit as AK
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSObject, NSTimer
import objc

from src.ui.notch import NotchWindow
from src.ui.chats import CHAT_PATH, ChatStore


async def _run_agent(commands, events, *, preview=False):
    stopped = asyncio.Event()

    async def wait_for_stop():
        await asyncio.to_thread(commands.get)
        stopped.set()

    stopping = asyncio.create_task(wait_for_stop())
    try:
        if preview:
            await stopped.wait()
        else:
            from src.agent.conversation import run_conversation

            await run_conversation(stopped=stopped, on_activity=events.put, on_audio=events.put)
    finally:
        commands.put(None)
        await stopping


class LunaApp(NSObject):
    @objc.python_method
    def build(self, commands, events):
        self.commands, self.events = commands, events
        self.notch = NotchWindow.alloc().init()
        self.notch.build(store=ChatStore())
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
            if isinstance(event, bool):
                self.notch.set_expanded(event)
            else:
                self.notch.set_audio(*event)

    def applicationShouldTerminate_(self, app):
        self.commands.put(None)
        return AK.NSTerminateCancel

    @objc.python_method
    def close(self):
        self.timer.invalidate()
        self.notch.close()


def run_app(*, preview=False):
    CHAT_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(CHAT_PATH.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600), "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Luna's island is already running. Stop that instance before opening another.") from None
        _run_app(preview=preview)


def _run_app(*, preview=False):
    commands, events, errors = queue.Queue(), queue.Queue(), []
    app = AK.NSApplication.sharedApplication()
    app.setActivationPolicy_(AK.NSApplicationActivationPolicyAccessory)
    controller = LunaApp.alloc().init()
    controller.build(commands, events)
    app.setDelegate_(controller)

    def run():
        try:
            asyncio.run(_run_agent(commands, events, preview=preview))
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
        controller.close()
        app.setDelegate_(None)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if errors:
        raise errors[0]
