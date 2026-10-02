"""Build the pinned Spotify MCP and configure it from .env. No Spotify actions or login."""

import hashlib
import json
import os
import platform
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


SPOTIFY_REVISION = "001fea0186ad8f40a0dad7f6f154a47e71962f85"
NODE_VERSION = "26.8.1"
DATA = Path.home() / "Library/Application Support/Luna"


def _node() -> Path:
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


def setup() -> None:
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    node = _node()
    directory = DATA / "servers/spotify"
    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--no-checkout", "https://github.com/marcelmarais/spotify-mcp-server.git",
                        str(directory)], check=True)
        subprocess.run(["git", "-C", str(directory), "checkout", "--detach", SPOTIFY_REVISION], check=True)
    revision = subprocess.check_output(["git", "-C", str(directory), "rev-parse", "HEAD"], text=True).strip()
    if revision != SPOTIFY_REVISION:
        raise RuntimeError("Existing Spotify checkout differs from the pinned revision; it was left untouched")
    env = os.environ | {"PATH": f"{node.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    subprocess.run([str(node.parent / "npm"), "ci"], cwd=directory, env=env, check=True)
    subprocess.run([str(node.parent / "npm"), "run", "build"], cwd=directory, env=env, check=True)

    credentials = directory / "spotify-config.json"
    if not credentials.exists():
        values = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
        required = ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI")
        if not all(values.get(k) for k in required):
            raise RuntimeError("Add SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, and SPOTIFY_REDIRECT_URI to .env")
        redirect = urlsplit(values["SPOTIFY_REDIRECT_URI"])
        if redirect.hostname == "localhost" and redirect.port:
            redirect = redirect._replace(netloc=f"127.0.0.1:{redirect.port}")
        if redirect.scheme != "http" or redirect.hostname != "127.0.0.1" or not redirect.port:
            raise RuntimeError("Spotify redirect must use http://127.0.0.1:<port>/callback and match the developer app")
        payload = {"clientId": values[required[0]], "clientSecret": values[required[1]],
                   "redirectUri": redirect.geturl()}
        with os.fdopen(os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            json.dump(payload, output, indent=2)
    os.chmod(credentials, 0o600)

    config_path = DATA / "mcp.toml"
    existing = config_path.read_text() if config_path.exists() else ""
    if "spotify" not in tomllib.loads(existing).get("integrations", {}):
        entry = '\n[integrations.spotify]\n'
        entry += 'description = "Spotify music, playback, and playlists"\n'
        entry += f'command = {json.dumps(str(node))}\n'
        entry += f'args = [{json.dumps(str(directory / "build/index.js"))}]\n'
        entry += f'cwd = {json.dumps(str(directory))}\n'
        entry += 'auth = "spotify"\n'
        entry += 'tools = ["searchSpotify", "getNowPlaying", "getMyPlaylists", "getAvailableDevices", "playMusic", "pausePlayback"]\n'
        config_path.write_text(existing + entry)
    print("Spotify MCP is built and configured. Start Luna to connect and sign in.")


if __name__ == "__main__":
    setup()
