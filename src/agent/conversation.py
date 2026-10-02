from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import StartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
)
from pipecat.transports.base_transport import BaseTransport
from pipecat.workers.runner import WorkerRunner

from src.agent.services import STT_LATENCY_BUDGET, build_llm_service, build_speech_services
from src.audio.apple import AppleAudioTransport
from src.integrations.mcp import MCPRuntime


def build_worker(transport: BaseTransport, integrations: MCPRuntime) -> PipelineWorker:
    """Connect audio, speech services, and the LLM with shared conversation history."""
    stt, tts = build_speech_services()
    llm = build_llm_service()
    if integrations.integrations:
        llm.append_system_instruction(
            "Use integration tools to carry out service requests. If their tools are missing, "
            "call connect_integration. When a tool returns auth_required, briefly explain that "
            "sign-in is needed and call authorize_integration. While awaiting_authorization, "
            "do not retry service tools or start another login. You can keep talking with the user. "
            "Only a connected result confirms authorization. When it arrives, continue the user's "
            "still-relevant task using tools; if the request changed, ask whether to continue. "
            "Respect denied, failed, timed_out, and cancelled outcomes. Never claim a service "
            "action succeeded without its result. Never read structured tool data aloud verbatim."
        )
    user, assistant = LLMContextAggregatorPair(
        LLMContext(tools=integrations.tools_schema()),
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            # Allow local batch STT to finish before the turn watchdog fires.
            user_turn_stop_timeout=STT_LATENCY_BUDGET + 5.0,
        ),
    )

    @user.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message: UserTurnStoppedMessage):
        if message.content:
            logger.info("You: {}", message.content)

    @assistant.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message: AssistantTurnStoppedMessage):
        if message.content:
            logger.info("Luna: {}", message.content)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user,
            llm,
            tts,
            transport.output(),
            assistant,
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
        enable_rtvi=False,
        idle_timeout_secs=None,
        processor_unusable_policy=ProcessorUnusablePolicy.CANCEL,
        app_resources=integrations,
    )

    @worker.event_handler("on_pipeline_started")
    async def on_pipeline_started(worker, frame: StartFrame):
        logger.info("Luna is listening. Start talking; press Ctrl+C to stop.")

    return worker


async def run_conversation() -> None:
    """Listen and respond through the Mac's default microphone and speakers."""
    transport = AppleAudioTransport()
    async with MCPRuntime.from_config() as integrations:
        worker = build_worker(transport, integrations)
        runner = WorkerRunner(handle_sigterm=True)
        await runner.add_workers(worker)
        await runner.run()
