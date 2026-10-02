import httpx


BASE_URL = "http://127.0.0.1:3900/v1"
# VoiceStudio requires the requested ASR engine to be active.
STT_ENGINE = "mlx-whisper"


async def text_to_speech(text: str, *, voice: str = "default") -> bytes:
    """Return WAV audio bytes from VoiceStudio's active TTS engine."""
    if not text.strip():
        raise ValueError("Text must not be empty.")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=120.0) as client:
        response = await client.post(
            "/audio/speech",
            json={
                "model": "tts-1",
                "input": text,
                "voice": voice,
                "response_format": "wav",
            },
        )
        response.raise_for_status()
        return response.content


async def speech_to_text(audio: bytes) -> str:
    """Transcribe WAV audio bytes with VoiceStudio's MLX Whisper engine."""
    if not audio:
        raise ValueError("Audio must not be empty.")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=120.0) as client:
        response = await client.post(
            "/audio/transcriptions",
            data={"model": STT_ENGINE, "response_format": "text"},
            files={"file": ("audio.wav", audio, "audio/wav")},
        )
        response.raise_for_status()
        return response.text
