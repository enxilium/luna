"""Install and supervise Luna's free local memory stack."""

import asyncio
import json
import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

from scripts.memory_service import MODEL
from src.memory import API_URL, CONFIG, DATA, Memory, memory_lock

ROOT = Path(__file__).resolve().parents[1]
AGENTS = Path.home() / "Library/LaunchAgents"
RUNTIME = DATA / "runtime"
LABEL = "dev.luna.memory"
OLLAMA_LABEL = "dev.luna.memory-ollama"


def stop_service(label):
    target = f"gui/{os.getuid()}/{label}"
    status = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
    pid = next((int(line.split("=")[1]) for line in status.stdout.splitlines()
                if line.strip().startswith("pid =")), None)
    subprocess.run(["launchctl", "bootout", target], capture_output=True)
    if pid:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(.1)
        raise RuntimeError(f"{label} has not stopped; no memory data was deleted.")


def service(label, arguments, environment=None):
    path = AGENTS / f"{label}.plist"
    spec = {
        "Label": label, "ProgramArguments": arguments, "WorkingDirectory": str(ROOT),
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "StandardOutPath": str(DATA / f"{label}.log"),
        "StandardErrorPath": str(DATA / f"{label}.log"),
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
                                 **(environment or {})},
        "Umask": 0o077,
    }
    path.write_bytes(plistlib.dumps(spec))
    path.chmod(0o600)
    domain = f"gui/{os.getuid()}"
    stop_service(label)
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)


def wait_for(url, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError(f"Local memory service did not become ready. See logs in {DATA}.")


def main():
    os.umask(0o077)
    uv = shutil.which("uv")
    ollama = shutil.which("ollama")
    if not uv or not ollama:
        raise RuntimeError("Install uv and Ollama first, then run this setup again.")
    AGENTS.mkdir(parents=True, exist_ok=True)
    with memory_lock(exclusive=True):
        if not (RUNTIME / "bin/python").exists():
            subprocess.run([uv, "venv", "--python", "3.13", str(RUNTIME)], check=True)
        subprocess.run([uv, "pip", "install", "--quiet", "--python", str(RUNTIME / "bin/python"),
                        "hindsight-api-slim[embedded-db,local-onnx]==1.1.0"], check=True)
        service(OLLAMA_LABEL, [ollama, "serve"], {
            "OLLAMA_HOST": "127.0.0.1:11435", "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_CONTEXT_LENGTH": "32768",
            "OLLAMA_NO_CLOUD": "1",
        })
        wait_for("http://127.0.0.1:11435/api/tags", 30)
        print("Preparing the local memory model (about 18 GB on first setup)…", flush=True)
        subprocess.run([ollama, "pull", MODEL],
                       env={**os.environ, "OLLAMA_HOST": "127.0.0.1:11435"}, check=True)
        service(LABEL, [str(RUNTIME / "bin/python"), "-m", "scripts.memory_service"])
        wait_for(API_URL + "/health")
        CONFIG.write_text(json.dumps({"version": 1, "bank": "luna", "engine": "hindsight-1.1.0"}) + "\n")
        CONFIG.chmod(0o600)
    async def initialize():
        async with Memory.open():
            pass
    asyncio.run(initialize())
    print("Luna's local memory is ready. Run uv run python main.py.")


if __name__ == "__main__":
    main()
