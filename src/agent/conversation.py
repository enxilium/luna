import asyncio
import signal
import time

from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import (
    PipelineParams,
    PipelineWorker,
    ProcessorUnusablePolicy,
)
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.transports.base_transport import BaseTransport
from pipecat.workers.runner import WorkerRunner

from src.agent.services import build_llm_service
from src.audio.apple import AppleAudioTransport
from src.audio.levels import pcm_levels
from src.audio.wake_word import SherpaDetector, WakeWordGate
from src.integrations.mcp import MCPRuntime
from src.memory import Memory


def build_worker(transport: BaseTransport, integrations: MCPRuntime, memory=None, *,
                 on_activity=None, on_audio=None) -> PipelineWorker:
    """Wake locally, then stream microphone audio through Gemini Live."""
    detector = SherpaDetector()
    llm = build_llm_service(integrations, defer_connection=True, memory=memory)
    active = False

    def activity(value):
        nonlocal active
        active = value
        if on_activity:
            on_activity(value)

    source, output = transport.input(), transport.output()
    if on_audio:
        def publish_audio(speaker, frame=None):
            if active:
                on_audio((speaker, pcm_levels(frame.audio) if frame else None, time.monotonic()))

        @source.event_handler("on_audio_captured")
        async def on_capture(transport, frame):
            publish_audio("user", frame)

        @output.event_handler("on_audio_delivered")
        async def on_playback(transport, frame):
            publish_audio("luna", frame)

        @output.event_handler("on_playback_stopped")
        async def on_playback_stopped(transport):
            publish_audio("luna")

    async def activate():
        activity(True)
        try:
            await llm.activate()
        except BaseException:
            activity(False)
            raise

    gate = WakeWordGate(detector, activate)

    @llm.event_handler("on_conversation_ended")
    async def on_conversation_ended(service):
        activity(False)
        await gate.rearm()

    @llm.event_handler("on_connection_lost")
    async def on_connection_lost(service):
        await gate.pause()

    @llm.event_handler("on_connection_ready")
    async def on_connection_ready(service):
        await gate.resume()

    context = llm.context
    user, assistant = LLMContextAggregatorPair(context)
    if memory:
        memory.attach(user, assistant)

    @user.event_handler("on_user_turn_message_added")
    async def on_user_turn(aggregator, message):
        if message.content:
            logger.info("You: {}", message.content)

    @assistant.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn(aggregator, message):
        if message.content:
            logger.info("Luna: {}", message.content)

    worker = PipelineWorker(
        Pipeline([
            source, gate,
            user, llm, output, assistant,
        ]),
        params=PipelineParams(
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        enable_rtvi=False,
        idle_timeout_secs=None,
        processor_unusable_policy=ProcessorUnusablePolicy.CANCEL,
        app_resources=integrations,
    )

    @worker.event_handler("on_pipeline_started")
    async def on_started(worker, frame):
        logger.info("Waiting for the wake word. Say Luna to start; press Ctrl+C to stop.")

    @worker.event_handler("on_pipeline_error")
    async def on_error(worker, frame):
        await worker.cancel()

    return worker


async def run_conversation(*, stopped=None, on_activity=None, on_audio=None) -> None:
    """Listen and respond through the Mac's default microphone and speakers."""
    owns_signals = stopped is None
    if stopped is None:
        stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = (
        {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        if owns_signals else {}
    )
    for sig in previous:
        loop.add_signal_handler(sig, stopped.set)
    try:
        async with MCPRuntime.from_config() as integrations, Memory.open() as memory:
            await _run_background(integrations, stopped, memory, on_activity=on_activity, on_audio=on_audio)
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


async def _run_background(integrations, stopped, memory=None, *, on_activity=None, on_audio=None):
    waiting_for_stop = asyncio.create_task(stopped.wait())
    delay = 1.0
    try:
        while not stopped.is_set():
            audio = runner = running = None
            started = time.monotonic()
            try:
                audio = AppleAudioTransport()
                worker = build_worker(audio, integrations, memory, on_activity=on_activity, on_audio=on_audio)
                runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
                await runner.add_workers(worker)
                running = asyncio.create_task(runner.run())
                await asyncio.wait((running, waiting_for_stop), return_when=asyncio.FIRST_COMPLETED)
                if stopped.is_set():
                    return
                running.result()
                logger.warning("Audio pipeline stopped; restarting local wake word listening.")
            except (ValueError, FileNotFoundError):
                raise
            except Exception as error:
                logger.warning("Audio pipeline could not run ({}); retrying.", type(error).__name__)
            finally:
                if on_activity:
                    on_activity(False)
                if runner and running and not running.done():
                    await runner.cancel()
                if running:
                    await asyncio.gather(running, return_exceptions=True)
                if audio:
                    try:
                        audio._close()
                    except Exception as error:
                        logger.error("Could not release Apple audio: {}", error)
            if time.monotonic() - started >= 10:
                delay = 1.0
            try:
                await asyncio.wait_for(asyncio.shield(waiting_for_stop), delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, 30.0)
    finally:
        waiting_for_stop.cancel()
        await asyncio.gather(waiting_for_stop, return_exceptions=True)
