"""Where Luna's own tools and files are: inside Luna.app when it's packaged (scripts/package_app.py), else this checkout
and your PATH (development). The installers in src/install ask here, so the same setup runs in both.

Packaged layout, under Luna.app/Contents/Resources:
    app/                     Luna's code (main.py, src/, scripts/)
    python/                  the Python Luna runs on, with its packages
    bin/uv                   for the memory service's and Canvas's own Python environments
    ollama/                  the Ollama CLI and its libraries (memory)
    models/ollama/           memory's model in Ollama's folder layout, when packaged --with-model (~18 GB)
    node/                    Node.js (the Spotify server)
    servers/spotify/         the pinned, patched Spotify MCP server, built
    credentials/             the OAuth apps friends sign in through: google-oauth-client.json, spotify.json
"""

import json
import os
import shutil
from pathlib import Path

from src.paths import DEFAULT_HOME, PACKAGED

APP = Path(__file__).resolve().parents[1]
BUNDLE = APP.parent if PACKAGED else None
PATCHES = APP / "scripts/patches"
_SEARCH = os.pathsep.join([os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin")])


def packaged():
    return BUNDLE is not None


def uv():
    path = BUNDLE / "bin/uv" if BUNDLE else shutil.which("uv", path=_SEARCH)
    return Path(path) if path and Path(path).exists() else None


def ollama():
    path = BUNDLE / "ollama/ollama" if BUNDLE else shutil.which("ollama", path=_SEARCH)
    return Path(path) if path and Path(path).exists() else None


def memory_model():
    """An Ollama models folder holding memory's model, to copy instead of downloading; only in a --with-model app."""
    from src.install.memory import model_files
    path = BUNDLE / "models/ollama" if BUNDLE else None
    return path if path and model_files(path) else None


def node():
    """The bundled Node, or None in development (the Spotify installer downloads the pinned one)."""
    path = BUNDLE / "node/bin/node" if BUNDLE else None
    return path if path and path.exists() else None


def spotify_server():
    """The prebuilt Spotify server to copy, or None in development (the installer builds it)."""
    path = BUNDLE / "servers/spotify" if BUNDLE else None
    return path if path and (path / "build/index.js").exists() else None


def google_client():
    """The Google OAuth web client (JSON bytes) people sign in through, or None.

    Packaged: the one the app was built with. Development: the one your own Luna imported (scripts/setup_google.py)."""
    path = BUNDLE / "credentials/google-oauth-client.json" if BUNDLE else DEFAULT_HOME / "google-oauth-client.json"
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


def spotify_app():
    """{"clientId", "clientSecret", "redirectUri"} of the Spotify app people sign in through, or None.

    Packaged: the one the app was built with. Development: SPOTIFY_* in the checkout's .env."""
    if BUNDLE:
        path = BUNDLE / "credentials/spotify.json"
        try:
            return json.loads(path.read_text()) if path.is_file() else None
        except (OSError, ValueError):
            return None
    env = APP / ".env"
    if not env.is_file():
        return None
    from dotenv import dotenv_values

    values = dotenv_values(env)
    app = {"clientId": values.get("SPOTIFY_CLIENT_ID"), "clientSecret": values.get("SPOTIFY_CLIENT_SECRET"),
           "redirectUri": values.get("SPOTIFY_REDIRECT_URI")}
    return app if all(app.values()) else None
