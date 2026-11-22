"""Spotify: the pinned, patched Spotify MCP server (prebuilt in Luna.app, built from source in development), configured
with the Spotify app people sign in through, then a sign-in in the browser. Tokens stay in the server's own folder."""

import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from src import resources
from src.install import InstallError
from src.paths import HOME

SPOTIFY_REVISION = "001fea0186ad8f40a0dad7f6f154a47e71962f85"
NODE_VERSION = "26.8.1"
DATA = HOME
SERVER = DATA / "servers/spotify"
CONFIG = SERVER / "spotify-config.json"
PLAYBACK_PATCH = resources.PATCHES / "spotify-playback.patch"
TOOLS = ["searchSpotify", "getNowPlaying", "getMyPlaylists", "getAvailableDevices", "playMusic", "pausePlayback"]


def _patch_server(directory: Path) -> None:
    if subprocess.run(
        ["git", "apply", "--reverse", "--check", str(PLAYBACK_PATCH)],
        cwd=directory, capture_output=True,
    ).returncode == 0:
        return
    subprocess.run(
        ["git", "apply", "--check", str(PLAYBACK_PATCH)], cwd=directory, check=True,
    )
    subprocess.run(["git", "apply", str(PLAYBACK_PATCH)], cwd=directory, check=True)


def _node() -> Path:
    bundled = resources.node()
    if bundled:
        return bundled
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("This setup targets Apple Silicon macOS")
    name = f"node-v{NODE_VERSION}-darwin-arm64"
    executable = DATA / "runtime" / name / "bin/node"
    if executable.exists():
        return executable
    archive = name + ".tar.xz"
    base = f"https://nodejs.org/dist/v{NODE_VERSION}/"
    checksums = urllib.request.urlopen(base + "SHASUMS256.txt", timeout=30).read().decode()
    expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == archive)
    runtime = DATA / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as temporary:
        path = Path(temporary) / archive
        with urllib.request.urlopen(base + archive, timeout=60) as response, path.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Node download checksum mismatch")
        with tarfile.open(path) as bundle:
            bundle.extractall(runtime, filter="data")
    return executable


def build(directory: Path, node: Path) -> None:
    """Clone the pinned revision, apply Luna's playback patch and build (development, and packaging)."""
    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--no-checkout", "https://github.com/marcelmarais/spotify-mcp-server.git",
                        str(directory)], check=True)
        subprocess.run(["git", "-C", str(directory), "checkout", "--detach", SPOTIFY_REVISION], check=True)
    revision = subprocess.check_output(["git", "-C", str(directory), "rev-parse", "HEAD"], text=True).strip()
    if revision != SPOTIFY_REVISION:
        raise RuntimeError("Existing Spotify checkout differs from the pinned revision; it was left untouched")
    _patch_server(directory)
    env = os.environ | {"PATH": f"{node.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    subprocess.run([str(node.parent / "npm"), "ci"], cwd=directory, env=env, check=True)
    subprocess.run([str(node.parent / "npm"), "run", "build"], cwd=directory, env=env, check=True)


def _server(node: Path) -> None:
    if (SERVER / "build/index.js").exists():
        return
    prebuilt = resources.spotify_server()
    if prebuilt:
        SERVER.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(prebuilt, SERVER, symlinks=True)
    else:
        build(SERVER, node)


def _credentials() -> None:
    if CONFIG.exists():
        os.chmod(CONFIG, 0o600)
        return
    app = resources.spotify_app()
    if not app:
        raise InstallError("spotify_app_missing")
    redirect = urlsplit(app["redirectUri"])
    if redirect.hostname == "localhost" and redirect.port:
        redirect = redirect._replace(netloc=f"127.0.0.1:{redirect.port}")
    if redirect.scheme != "http" or redirect.hostname != "127.0.0.1" or not redirect.port:
        raise InstallError("spotify_app_invalid")
    payload = {"clientId": app["clientId"], "clientSecret": app["clientSecret"], "redirectUri": redirect.geturl()}
    with os.fdopen(os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(payload, output, indent=2)


def register(node: Path) -> None:
    config_path = DATA / "mcp.toml"
    existing = config_path.read_text() if config_path.exists() else ""
    if "spotify" in tomllib.loads(existing).get("integrations", {}):
        return
    entry = '\n[integrations.spotify]\n'
    entry += 'description = "Spotify music, playback, and playlists"\n'
    entry += f'command = {json.dumps(str(node))}\n'
    entry += f'args = [{json.dumps(str(SERVER / "build/index.js"))}]\n'
    entry += f'cwd = {json.dumps(str(SERVER))}\n'
    entry += 'auth = "spotify"\n'
    entry += f'tools = {json.dumps(TOOLS)}\n'
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path.write_text(existing + entry)


def setup() -> Path:
    """Server, credentials and registration; no sign-in. Returns the Node it runs on."""
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    node = _node()
    _server(node)
    _credentials()
    register(node)
    return node


def ready():
    try:
        config = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return False
    return bool(config.get("refreshToken"))


def install(job):
    from src.integrations.spotify import SpotifyAuthAdapter

    job.report("Preparing Spotify")
    try:
        node = setup()
    except InstallError:
        raise
    except Exception:
        raise InstallError("spotify_install_failed") from None
    job.report("Waiting for you to sign in to Spotify in your browser")

    async def progress(update):
        pass

    try:
        outcome = asyncio.run(asyncio.wait_for(SpotifyAuthAdapter(SERVER, str(node)).authorize(progress), 295))
    except TimeoutError:
        raise InstallError("sign_in_timed_out") from None
    if outcome.status != "connected":
        raise InstallError({"denied": "sign_in_cancelled"}.get(outcome.status, outcome.reason or "sign_in_failed"))
