"""Shared command-line synthesis and macOS playback for the two TTS tests."""

import argparse
from pathlib import Path
import subprocess
import tempfile
import time

import httpx

from src.clients.voicestudio import BASE_URL


def run(model: str) -> None:
    parser = argparse.ArgumentParser(
        description=f"Test VoiceStudio {model} with alloy."
    )
    parser.add_argument("text", help="Text to synthesize (wrap it in quotes).")
    args = parser.parse_args()
    if not args.text.strip():
        parser.error("Text must not be empty.")

    print(f"Synthesizing with model={model}, voice=alloy...", flush=True)
    try:
        started = time.perf_counter()
        response = httpx.post(
            f"{BASE_URL}/audio/speech",
            json={
                "model": model,
                "voice": "alloy",
                "input": args.text,
                "response_format": "wav",
            },
            timeout=120.0,
        )
        response.raise_for_status()
        print(
            f"Synthesis took {time.perf_counter() - started:.2f} seconds.", flush=True
        )

        with tempfile.TemporaryDirectory(prefix="luna-tts-") as directory:
            audio_path = Path(directory) / "speech.wav"
            audio_path.write_bytes(response.content)
            subprocess.run(["afplay", str(audio_path)], check=True)
    except (httpx.HTTPError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Synthesis or playback failed: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "\nStopped.\n")
