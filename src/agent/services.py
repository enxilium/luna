import asyncio
from contextlib import aclosing
from time import perf_counter

from loguru import logger
from pipecat.frames.frames import (
    CancelFrame, ErrorFrame, InterruptionFrame, LLMFullResponseEndFrame,
    LLMFullResponseStartFrame, LLMTextFrame, TTSAudioRawFrame,
)
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.services.tts_service import TextAggregationMode

from src.clients import ollama, voicestudio


STT_LATENCY_BUDGET = 10.0


class _LoggedTTSService(OpenAITTSService):
    async def run_tts(self, text: str, context_id: str):
        started, first_audio, outcome = perf_counter(), False, "completed"
        logger.info("TTS synthesis [{}] started ({} characters)", context_id, len(text))
        try:
            async with aclosing(super().run_tts(text, context_id)) as frames:
                async for frame in frames:
                    if isinstance(frame, TTSAudioRawFrame) and frame.audio and not first_audio:
                        first_audio = True
                        logger.info("TTS synthesis [{}] first audio after {:.3f}s", context_id, perf_counter() - started)
                    elif isinstance(frame, ErrorFrame):
                        outcome = "failed"
                    yield frame
        except (asyncio.CancelledError, GeneratorExit):
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "failed"
            raise
        finally:
            logger.info("TTS synthesis [{}] ended: {:.3f}s ({})", context_id, perf_counter() - started, outcome)


def build_llm_service() -> OLLamaLLMService:
    """Stream local Ollama responses through Pipecat's native connector."""
    llm = OLLamaLLMService(
        base_url=f"{ollama.BASE_URL}/v1",
        settings=OLLamaLLMService.Settings(
            model=ollama.MODEL,
            system_instruction=(
                """
                You are Luna, an immensely capable, JARVIS-like assistant. Keep sentences succint and direct unless otherwise told. Your output will be read aloud via voice, so output plain text only, no markdown or other structured formats.
                """
            ),
            extra={"reasoning_effort": "none"},
        ),
    )
    started, first_text, inference, outcome = None, False, 0, "completed"

    @llm.event_handler("on_before_process_frame")
    async def on_process(service, frame):
        nonlocal outcome
        if started is not None and isinstance(frame, (InterruptionFrame, CancelFrame)):
            outcome = "interrupted" if isinstance(frame, InterruptionFrame) else "cancelled"

    @llm.event_handler("on_before_push_frame")
    async def on_output(service, frame):
        nonlocal started, first_text, inference, outcome
        if isinstance(frame, LLMFullResponseStartFrame):
            started, first_text, outcome = perf_counter(), False, "completed"
            inference += 1
            logger.info("LLM inference #{} started", inference)
        elif started is not None:
            if isinstance(frame, LLMTextFrame) and frame.text.strip() and not first_text:
                first_text = True
                logger.info("LLM inference #{} first text after {:.3f}s", inference, perf_counter() - started)
            elif isinstance(frame, ErrorFrame):
                outcome = "failed"
            elif isinstance(frame, LLMFullResponseEndFrame):
                logger.info("LLM inference #{} ended: {:.3f}s ({})", inference, perf_counter() - started, outcome)
                started = None

    return llm


def build_speech_services() -> tuple[OpenAISTTService, OpenAITTSService]:
    """Use VoiceStudio's MLX Whisper and MLX-Audio engines."""
    stt = OpenAISTTService(
        base_url=voicestudio.BASE_URL,
        api_key="local",
        settings=OpenAISTTService.Settings(model=voicestudio.STT_ENGINE),
        ttfs_p99_latency=STT_LATENCY_BUDGET,
    )
    return stt, build_tts_service()


def build_tts_service() -> OpenAITTSService:
    """Use the same MLX-Audio settings for voice and typed conversations."""
    return _LoggedTTSService(
        base_url=voicestudio.BASE_URL,
        api_key="local",
        sample_rate=24000,
        text_aggregation_mode=TextAggregationMode.SENTENCE,
        # Batch synthesis can outlast Pipecat's default 3-second audio idle timeout.
        stop_frame_timeout_s=120.0,
        # VoiceStudio maps alloy to MLX-Audio's default voice.
        settings=OpenAITTSService.Settings(model="mlx-audio", voice="alloy"),
    )
