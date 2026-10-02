from collections.abc import AsyncIterator

from ollama import AsyncClient


MODEL = "qwen3.8:27b-q4_K_M"
BASE_URL = "http://127.0.0.1:11434"


async def stream_response(text: str) -> AsyncIterator[str]:
    """Yield a text-only reply from the local Ollama model as it generates."""
    if not text.strip():
        raise ValueError("Text must not be empty.")

    async with AsyncClient(host=BASE_URL, timeout=120.0) as client:
        stream = await client.chat(
            model=MODEL,
            messages=[{"role": "user", "content": text}],
            stream=True,
            think=False,
            options={"num_ctx": 8192},
        )
        async for chunk in stream:
            if chunk.message.content:
                yield chunk.message.content
