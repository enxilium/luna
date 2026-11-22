"""Reset Luna-owned memory; stop Luna first. Run with --yes to confirm deletion."""

import argparse
import os
import subprocess
from pathlib import Path

from src.install.memory import AGENTS, OLLAMA_URL, RUNTIME, stop_service, wait_for
from src.memory import API_URL, CONFIG, DATA, memory_lock
from src.paths import MEMORY_LABEL as LABEL, OLLAMA_LABEL


def reset():
    if not CONFIG.exists():
        raise RuntimeError("Luna memory has not been set up.")
    with memory_lock(exclusive=True):
        stop_service(LABEL)
        stop_service(OLLAMA_LABEL)
        command = "from scripts.memory_service import drop_database; drop_database()"
        subprocess.run([str(RUNTIME / "bin/python"), "-c", command], check=True,
                       cwd=Path(__file__).resolve().parents[1])
        for label in (LABEL, OLLAMA_LABEL):
            log = DATA / f"{label}.log"
            if log.exists():
                log.write_text("")
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(AGENTS / f"{OLLAMA_LABEL}.plist")], check=True)
        wait_for(OLLAMA_URL + "/api/tags", 30)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(AGENTS / f"{LABEL}.plist")], check=True)
        wait_for(API_URL + "/health")
    import asyncio

    from src.memory import Memory
    async def initialize():
        async with Memory.open():
            pass
    asyncio.run(initialize())


def main():
    parser = argparse.ArgumentParser(description="Permanently delete all Luna memories, transcripts and pending ingestion. Models and integration credentials are preserved.")
    parser.add_argument("--yes", action="store_true", help="Confirm permanent deletion of all Luna memory")
    args = parser.parse_args()
    if not args.yes:
        parser.error("Stop Luna, then pass --yes to permanently reset all memory.")
    reset()
    print("All Luna memories and source transcripts have been reset. Models and integration credentials are unchanged.")


if __name__ == "__main__":
    main()
