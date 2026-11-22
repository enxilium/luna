"""Download the pinned wake word model once, outside conversation startup (Luna's first-run setup does the same)."""

from src.audio.wake_word import MODEL_DIR, MODEL_NAME, download_model, model_ready


if __name__ == "__main__":
    if model_ready():
        print(f"Wake word model already available: {MODEL_DIR}")
    else:
        print(f"Downloading {MODEL_NAME}…", flush=True)
        download_model()
        print(f"Wake word model ready: {MODEL_DIR}")
