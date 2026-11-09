"""Download the pinned wake word model once, outside conversation startup."""

import hashlib
from pathlib import Path
import tarfile
import tempfile
from urllib.request import urlopen

from src.audio.wake_word import MODEL_DIR, MODEL_FILES, MODEL_NAME


MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    f"{MODEL_NAME}.tar.bz2"
)
MODEL_SHA256 = "68447f4fbc67e70eee3a93961f36e81e98f47aef73ce7e7ca00885c6cd3616a6"


def download_model():
    if MODEL_DIR.exists():
        if not all((MODEL_DIR / name).is_file() for name in MODEL_FILES):
            raise RuntimeError(f"Incomplete model directory: {MODEL_DIR}")
        print(f"Wake word model already available: {MODEL_DIR}")
        return

    MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=MODEL_DIR.parent) as staging:
        archive = Path(staging) / "model.tar.bz2"
        digest = hashlib.sha256()
        print(f"Downloading {MODEL_NAME}…", flush=True)
        with urlopen(MODEL_URL, timeout=60) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != MODEL_SHA256:
            raise RuntimeError("Model checksum mismatch; download discarded.")
        with tarfile.open(archive) as bundle:
            bundle.extractall(staging, filter="data")
        extracted = Path(staging) / MODEL_NAME
        if not all((extracted / name).is_file() for name in MODEL_FILES):
            raise RuntimeError("The downloaded model is missing required files.")
        extracted.rename(MODEL_DIR)
    print(f"Wake word model ready: {MODEL_DIR}")


if __name__ == "__main__":
    download_model()
