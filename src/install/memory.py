"""Local memory: Hindsight (with its own Postgres) in its own Python 3.13, and Ollama serving the memory model, both kept
running by launchd. The model (~18 GB) comes with Luna.app when it was packaged --with-model, else from Ollama's registry;
either way it ends up once in ~/.ollama, where Ollama keeps it."""

import asyncio
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

from src import resources
from src.install import InstallError
from src.paths import HOME, MEMORY_API_PORT, MEMORY_LABEL, MEMORY_MODEL, OLLAMA_LABEL, OLLAMA_PORT, separate_home

DATA = HOME / "memory"
CONFIG = DATA / "config.json"
RUNTIME = DATA / "runtime"
AGENTS = Path.home() / "Library/LaunchAgents"
OLLAMA_URL = f"http://127.0.0.1:{OLLAMA_PORT}"
API_URL = f"http://127.0.0.1:{MEMORY_API_PORT}"
HINDSIGHT = "hindsight-api-slim[embedded-db,local-onnx]==1.1.0"
ROOM_FOR_MODEL = 22_000_000_000  # the model plus headroom
OLLAMA_MODELS = Path.home() / ".ollama/models"


def ready():
    return CONFIG.exists()


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
        "Label": label, "ProgramArguments": [str(a) for a in arguments], "WorkingDirectory": str(resources.APP),
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "StandardOutPath": str(DATA / f"{label}.log"),
        "StandardErrorPath": str(DATA / f"{label}.log"),
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
            # Never write bytecode into Luna.app (it would break the app's signature).
            "PYTHONDONTWRITEBYTECODE": "1",
            **({"LUNA_HOME": str(HOME)} if separate_home() else {}),
            **(environment or {}),
        },
        "Umask": 0o077,
    }
    path.write_bytes(plistlib.dumps(spec))
    path.chmod(0o600)
    stop_service(label)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)


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
    raise InstallError("service_unavailable")


def model_present():
    with urlopen(OLLAMA_URL + "/api/tags", timeout=10) as response:
        names = {m.get("name") for m in json.load(response).get("models", [])}
    return MEMORY_MODEL in names


def pull_model(report):
    """Ollama's own download, followed through its streaming API: one JSON line per update."""
    request = Request(OLLAMA_URL + "/api/pull", data=json.dumps({"model": MEMORY_MODEL, "stream": True}).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=60) as response:
        for line in response:
            update = json.loads(line or b"{}")
            if update.get("error"):
                raise InstallError("download_failed")
            if update.get("total"):
                report(done=update.get("completed", 0), total=update["total"])
            if update.get("status") == "success":
                return
    raise InstallError("download_failed")


def model_files(models=OLLAMA_MODELS):
    """The model's manifest and the blobs it names, relative to an Ollama models folder ([] if it isn't there)."""
    name, tag = MEMORY_MODEL.split(":")
    manifest = Path("manifests/registry.ollama.ai/library") / name / tag
    try:
        spec = json.loads((models / manifest).read_text())
    except (OSError, ValueError):
        return []
    blobs = list(dict.fromkeys(Path("blobs") / layer["digest"].replace(":", "-")
                               for layer in [spec["config"], *spec["layers"]]))
    return [manifest, *blobs] if all((models / blob).is_file() for blob in blobs) else []


def copy_model(source, report, target=OLLAMA_MODELS):
    """Copy the bundled model into Ollama's folder, checking every blob's digest; the manifest goes last, so Ollama
    never sees a model with missing parts."""
    files = model_files(source)
    manifest, blobs = files[0], files[1:]
    total, done = sum((source / blob).stat().st_size for blob in blobs), 0
    for blob in blobs:
        destination = target / blob
        if destination.is_file() and destination.stat().st_size == (source / blob).stat().st_size:
            done += destination.stat().st_size
            report(done=done, total=total)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, partial = hashlib.sha256(), destination.with_name(destination.name + "-partial")
        with (source / blob).open("rb") as reading, partial.open("wb") as writing:
            while chunk := reading.read(8 * 1024 * 1024):
                digest.update(chunk)
                writing.write(chunk)
                done += len(chunk)
                report(done=done, total=total)
        if "sha256-" + digest.hexdigest() != blob.name:
            partial.unlink()
            raise InstallError("model_damaged")
        partial.replace(destination)
    (target / manifest).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / manifest, target / manifest)


def _run(command, **options):
    result = subprocess.run([str(c) for c in command], capture_output=True, text=True, **options)
    if result.returncode != 0:
        raise InstallError("install_failed")


def install(job):
    os.umask(0o077)
    uv, ollama = resources.uv(), resources.ollama()
    if not uv or not ollama:
        raise InstallError("tools_missing")
    from src.memory import Memory, memory_lock

    AGENTS.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with memory_lock(exclusive=True):
        job.report("Preparing the memory service")
        if not (RUNTIME / "bin/python").exists():
            _run([uv, "venv", "--python", "3.13", RUNTIME])
        _run([uv, "pip", "install", "--quiet", "--python", RUNTIME / "bin/python", HINDSIGHT])
        job.report("Starting the model server")
        service(OLLAMA_LABEL, [ollama, "serve"], {
            "OLLAMA_HOST": f"127.0.0.1:{OLLAMA_PORT}", "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_CONTEXT_LENGTH": "32768", "OLLAMA_NO_CLOUD": "1",
            # Free the ~19 GB model a minute after extraction stops; Hindsight's requests arrive back to back.
            "OLLAMA_KEEP_ALIVE": "1m",
        })
        wait_for(OLLAMA_URL + "/api/tags", 30)
        if not model_present():
            if shutil.disk_usage(Path.home()).free < ROOM_FOR_MODEL:
                raise InstallError("disk_full")
            bundled = resources.memory_model()
            if bundled:
                job.report("Setting up the memory model")
                copy_model(bundled, job.report)
            else:
                job.report("Downloading the memory model")
                pull_model(job.report)
        job.report("Starting memory")
        service(MEMORY_LABEL, [RUNTIME / "bin/python", "-m", "scripts.memory_service"])
        # The first start also fetches Hindsight's small embedding models.
        wait_for(API_URL + "/health", 900)
        CONFIG.write_text(json.dumps({"version": 1, "bank": "luna", "engine": "hindsight-1.1.0"}) + "\n")
        CONFIG.chmod(0o600)

    async def initialize():
        async with Memory.open():
            pass

    asyncio.run(initialize())
