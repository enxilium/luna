"""Where Luna keeps its data, and where its local services listen. LUNA_HOME moves all of it, e.g. to try Luna as a new
user without touching your own.

Imported by the memory service too, which runs in its own Python with only Hindsight: keep this module dependency-free.
"""

import hashlib
import os
from pathlib import Path

DEFAULT_HOME = Path.home() / "Library/Application Support/Luna"
HOME = Path(os.environ.get("LUNA_HOME") or DEFAULT_HOME).expanduser()


def separate_home():
    """True when LUNA_HOME points somewhere other than the usual folder: a profile of its own, sharing nothing."""
    return HOME.resolve() != DEFAULT_HOME.resolve()


# The installed app runs its code from Luna.app/Contents/Resources/app; a development checkout (main.py, build/Luna.app)
# runs it from the repository. Both share the data folder, but each keeps its own Keychain entries ("Luna MCP" and
# "Luna MCP (dev)"), so clearing a dev sign-in to try setup again never touches the installed Luna's.
_CODE = Path(__file__).resolve().parents[1]
PACKAGED = _CODE.name == "app" and (_CODE.parent / "bin").is_dir()


def keychain_service(name):
    return name if PACKAGED else f"{name} (dev)"


# "" for your own Luna; a short name for a separate home, which tags its services, ports and Keychain entries.
PROFILE = hashlib.sha256(str(HOME.resolve()).encode()).hexdigest()[:8] if separate_home() else ""

# Local memory: Hindsight (with its own Postgres) and Ollama, run by launchd. A separate home gets its own ports and
# service names, so trying setup as a new user never touches your memory; Ollama's model files (~/.ollama) are shared,
# so the model isn't downloaded twice.
MEMORY_MODEL = "qwen3.8:27b-q4_K_M"
_OFFSET = 100 + int(PROFILE[:4], 16) % 400 if PROFILE else 0
MEMORY_API_PORT, MEMORY_DATABASE_PORT, OLLAMA_PORT = 8889 + _OFFSET, 55439 + _OFFSET, 11435 + _OFFSET
MEMORY_LABEL = "dev.luna.memory" + (f".{PROFILE}" if PROFILE else "")
OLLAMA_LABEL = "dev.luna.memory-ollama" + (f".{PROFILE}" if PROFILE else "")
MEMORY_DATABASE = "luna-memory" + (f"-{PROFILE}" if PROFILE else "")
