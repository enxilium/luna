"""Run with: uv run python -m scripts.inference_tts"""

import asyncio
import queue
import threading

import AppKit as AK
from Foundation import NSAttributedString, NSMakeRange, NSMakeRect, NSMakeSize, NSObject, NSTimer
import objc
from loguru import logger

from pipecat.frames.frames import LLMTextFrame, LLMMessagesAppendFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from src.agent.services import build_llm_service, build_tts_service
from src.audio.apple import AppleAudioTransport


class ReplyDisplay(FrameProcessor):
    def __init__(self, events: queue.Queue):
        super().__init__()
        self.events = events

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMTextFrame):
            self.events.put(("text", frame.text))
        await self.push_frame(frame, direction)


async def run_pipeline(commands: queue.Queue, events: queue.Queue) -> None:
    audio = AppleAudioTransport(audio_in_enabled=False)
    user, assistant = LLMContextAggregatorPair(
        LLMContext(),
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=ExternalUserTurnStrategies(enable_interruptions=False),
        ),
    )
    worker = PipelineWorker(
        Pipeline([
            user, build_llm_service(), ReplyDisplay(events), build_tts_service(),
            audio.output(), assistant,
        ]),
        params=PipelineParams(audio_out_sample_rate=24000),
        enable_rtvi=False,
        idle_timeout_secs=None,
        processor_unusable_policy=ProcessorUnusablePolicy.CANCEL,
    )

    @worker.event_handler("on_pipeline_started")
    async def on_started(worker, frame):
        events.put(("ready", ""))

    @worker.event_handler("on_pipeline_error")
    async def on_error(worker, frame):
        events.put(("error", frame.error))
        await worker.cancel()

    @assistant.event_handler("on_assistant_turn_stopped")
    async def on_reply(aggregator, message):
        events.put(("ready", ""))

    async def send_messages():
        while (text := await asyncio.to_thread(commands.get)) is not None:
            await worker.queue_frame(
                LLMMessagesAppendFrame([{"role": "user", "content": text}], run_llm=True)
            )

    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    running = asyncio.create_task(runner.run())
    sending = asyncio.create_task(send_messages())
    try:
        done, _ = await asyncio.wait({running, sending}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        commands.put(None)
        sending.cancel()
        await worker.cancel()
        await asyncio.gather(running, sending, return_exceptions=True)


class ChatWindow(NSObject):
    @objc.python_method
    def build(self, commands: queue.Queue, events: queue.Queue):
        self.commands = commands
        self.events = events
        self.busy = False
        self.failed = False
        style = AK.NSWindowStyleMaskTitled | AK.NSWindowStyleMaskClosable | AK.NSWindowStyleMaskMiniaturizable
        self.window = AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 720, 520), style, AK.NSBackingStoreBuffered, False
        )
        self.window.setTitle_("Luna — Inference + TTS")
        self.window.setDelegate_(self)
        self.window.setReleasedWhenClosed_(False)
        view = self.window.contentView()

        scroll = AK.NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 105, 680, 395))
        scroll.setHasVerticalScroller_(True)
        self.transcript = AK.NSTextView.alloc().initWithFrame_(scroll.contentView().bounds())
        self.transcript.setEditable_(False)
        self.transcript.setVerticallyResizable_(True)
        self.transcript.setHorizontallyResizable_(False)
        self.transcript.setMaxSize_(NSMakeSize(680, 10_000_000))
        self.transcript.textContainer().setContainerSize_(NSMakeSize(680, 10_000_000))
        self.transcript.textContainer().setWidthTracksTextView_(True)
        self.transcript.setFont_(AK.NSFont.systemFontOfSize_(14))
        scroll.setDocumentView_(self.transcript)
        view.addSubview_(scroll)

        self.input = AK.NSTextField.alloc().initWithFrame_(NSMakeRect(20, 60, 580, 28))
        self.input.setPlaceholderString_("Type a message, then press Enter")
        self.input.setTarget_(self)
        self.input.setAction_("send:")
        view.addSubview_(self.input)
        self.send_button = AK.NSButton.alloc().initWithFrame_(NSMakeRect(615, 58, 85, 32))
        self.send_button.setTitle_("Send")
        self.send_button.setBezelStyle_(AK.NSBezelStyleRounded)
        self.send_button.setTarget_(self)
        self.send_button.setAction_("send:")
        self.send_button.setEnabled_(False)
        view.addSubview_(self.send_button)

        self.status = AK.NSTextField.labelWithString_("Starting local pipeline…")
        self.status.setFrame_(NSMakeRect(20, 20, 680, 24))
        view.addSubview_(self.status)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self, "poll:", None, True
        )
        self.window.center()
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self.input)

    @objc.python_method
    def append(self, text: str):
        self.transcript.textStorage().appendAttributedString_(
            NSAttributedString.alloc().initWithString_(text)
        )
        self.transcript.scrollRangeToVisible_(NSMakeRange(self.transcript.textStorage().length(), 0))

    def send_(self, sender):
        text = self.input.stringValue().strip()
        if not text or not self.send_button.isEnabled():
            return
        self.busy = True
        self.send_button.setEnabled_(False)
        self.status.setStringValue_("Generating and speaking…")
        self.append(f"You: {text}\n\nLuna: ")
        self.input.setStringValue_("")
        self.commands.put(text)

    def poll_(self, timer):
        while True:
            try:
                kind, text = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "text":
                self.append(text)
            elif kind == "ready" and not self.failed:
                if self.busy:
                    self.append("\n\n")
                self.busy = False
                self.send_button.setEnabled_(True)
                self.status.setStringValue_("Ready — microphone disabled")
            elif kind == "error":
                self.failed = True
                self.send_button.setEnabled_(False)
                self.status.setStringValue_("Pipeline stopped — restart after fixing the error")
                self.append(f"\n\nError: {text}\n")

    def windowWillClose_(self, notification):
        self.timer.invalidate()
        self.commands.put(None)
        AK.NSApplication.sharedApplication().stop_(None)


def main() -> None:
    commands = queue.Queue()
    events = queue.Queue()

    def run():
        try:
            asyncio.run(run_pipeline(commands, events))
        except Exception as error:
            logger.exception("Inference/TTS pipeline failed")
            events.put(("error", str(error)))

    app = AK.NSApplication.sharedApplication()
    app.setActivationPolicy_(AK.NSApplicationActivationPolicyRegular)
    window = ChatWindow.alloc().init()
    window.build(commands, events)
    background = threading.Thread(target=run, name="luna-inference-tts")
    background.start()
    app.activateIgnoringOtherApps_(True)
    try:
        app.run()
    finally:
        commands.put(None)
        background.join()


if __name__ == "__main__":
    main()
