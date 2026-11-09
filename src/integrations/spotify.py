"""Result compatibility and authorization for the pinned Spotify MCP server."""

import asyncio
import json
import os
import re
import time
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from src.integrations.auth import AuthFailure, AuthOutcome, AuthRequired, Progress

_KINDS = ("track", "album", "artist", "playlist")
_URI = re.compile(r"spotify:(track|album|artist|playlist):([A-Za-z0-9]+)")
_URL = re.compile(
    r"https://open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|artist|playlist)/([A-Za-z0-9]+)(?:\?[^\s]*)?"
)
_RESULT_TYPES = {
    "getMyPlaylists": "playlist",
    "getPlaylist": "playlist",
    "createPlaylist": "playlist",
    "getNowPlaying": "track",
    "getRecentlyPlayed": "track",
    "getUsersSavedTracks": "track",
    "getTopTracks": "track",
    "getAlbumTracks": "track",
    "getAlbums": "album",
    "getTopArtists": "artist",
}


def playback_uri(arguments):
    value = arguments.get("uri")
    if value is None:
        value = arguments.get("context_uri")
    if not value and arguments.get("type") and arguments.get("id"):
        value = f"spotify:{arguments['type']}:{arguments['id']}"
    match = _URI.fullmatch(value) if isinstance(value, str) else None
    if match and arguments.get("type", match[1]) == match[1]:
        return value
    return None


def result_error(result, name=None):
    errors = [
        c.text
        for c in result.content
        if isinstance(c, TextContent)
        and (
            result.is_error
            or re.match(r"\s*Error(?:\s|:)", c.text, re.IGNORECASE)
            or getattr(c, "isError", False)
            or (
                name == "adjustVolume"
                and c.text
                in {
                    "No active device found. Make sure Spotify is open and playing on a device.",
                    "Unable to get current volume from device.",
                }
            )
        )
    ]
    if errors:
        result.is_error = True
    return " ".join(errors)


def result_identifiers(name, arguments, result):
    if result.is_error:
        return set()
    kind = arguments.get("type") if name == "searchSpotify" else _RESULT_TYPES.get(name)
    if name not in {"searchSpotify", "getPlaylistTracks", "getQueue", *_RESULT_TYPES}:
        return set()
    identifiers = set()

    def remember(value, default_kind=None):
        if isinstance(value, dict):
            item_kind = value.get("type", default_kind)
            item_id = value.get("id")
            if (
                item_kind in _KINDS
                and isinstance(item_id, str)
                and re.fullmatch(r"[A-Za-z0-9]+", item_id)
            ):
                identifiers.add(f"spotify:{item_kind}:{item_id}")
            for key, item in value.items():
                if key in {"uri", "context_uri", "spotify"} and isinstance(item, str):
                    uri = _URI.fullmatch(item) or _URL.fullmatch(item)
                    if uri:
                        identifiers.add(f"spotify:{uri[1]}:{uri[2]}")
                child_kind = key.removesuffix("s")
                if child_kind not in _KINDS:
                    child_kind = (
                        item_kind
                        if key
                        in {"items", "item", "results", "currently_playing", "queue"}
                        else None
                    )
                remember(item, child_kind)
        elif isinstance(value, list):
            for item in value:
                remember(item, default_kind)

    remember(result.structured_content, kind)
    for item in result.content:
        if not isinstance(item, TextContent):
            continue
        try:
            remember(json.loads(item.text), kind)
            continue
        except ValueError:
            pass
        # Only consume the pinned server's ID fields, never IDs mentioned in names or descriptions.
        if kind in _KINDS:
            for item_id in re.findall(
                r"(?m)^(?:\d+\. [^\n]* - ID: |\*\*ID\*\*: |Playlist ID: )([A-Za-z0-9]+)(?: - (?:Played at|Added): [^\n]*)?$",
                item.text,
            ):
                if item_id not in {"undefined", "null", "Unknown"}:
                    identifiers.add(f"spotify:{kind}:{item_id}")
    return identifiers


class SpotifyAuthAdapter:
    http_auth = None

    def __init__(self, server_directory: Path, node: str):
        self.directory = server_directory.resolve()
        self.node = node
        self.config_path = self.directory / "spotify-config.json"

    def _config(self) -> dict:
        if not self.config_path.is_file():
            raise AuthFailure("spotify_configuration_missing")
        try:
            os.chmod(self.config_path, 0o600)
            config = json.loads(self.config_path.read_text())
        except (ValueError, OSError):
            raise AuthFailure("spotify_configuration_invalid") from None
        if not isinstance(config, dict) or not all(
            isinstance(config.get(k), str) and config[k]
            for k in ("clientId", "clientSecret", "redirectUri")
        ):
            raise AuthFailure("spotify_configuration_invalid")
        return config

    def classify_failure(self, failure) -> AuthRequired | None:
        if isinstance(failure, AuthRequired):
            return failure
        if isinstance(failure, CallToolResult):
            message = result_error(failure)
            if not message:
                return None
        else:
            message = str(failure)
        message = message.lower()
        try:
            config = self._config()
        except AuthFailure:
            return None
        if not config.get("accessToken") or not config.get("refreshToken"):
            return AuthRequired()
        if "invalid_grant" in message or "no access token available" in message:
            return AuthRequired()
        if any(
            s in message for s in ("insufficient client scope", "insufficient_scope")
        ):
            return AuthRequired("additional_scope_required")
        if (
            re.search(r"\b401\b", message)
            or "valid user authentication required" in message
        ):
            return AuthRequired()
        # Upstream retains tokens on transient refresh failure, but removes them on invalid_grant.
        return None

    async def authorize(self, report_progress: Progress) -> AuthOutcome:
        try:
            config = self._config()
        except AuthFailure as error:
            return AuthOutcome("failed", str(error))
        from urllib.parse import urlsplit

        redirect = urlsplit(config["redirectUri"])
        if (
            redirect.scheme != "http"
            or redirect.hostname != "127.0.0.1"
            or not redirect.port
        ):
            return AuthOutcome("failed", "spotify_callback_must_use_loopback_ip")
        script = self.directory / "build/auth.js"
        if not script.is_file():
            return AuthOutcome("failed", "spotify_server_not_built")
        await report_progress({"status": "awaiting_authorization"})
        try:
            process = await asyncio.create_subprocess_exec(
                self.node,
                str(script),
                cwd=self.directory,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            return AuthOutcome("failed", "spotify_auth_process_unavailable")
        try:
            stdout, stderr = await process.communicate()
            if process.returncode:
                if b"access_denied" in stdout + stderr:
                    return AuthOutcome("denied")
                return AuthOutcome("failed", "spotify_authorization_failed")
            config = self._config()
            if (
                not all(
                    isinstance(config.get(k), str) and config[k]
                    for k in ("accessToken", "refreshToken")
                )
                or not isinstance(config.get("expiresAt"), (int, float))
                or not config["expiresAt"] > time.time() * 1000
            ):
                return AuthOutcome("failed", "spotify_credentials_not_saved")
            return AuthOutcome("connected")
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.communicate(), 3)
                except TimeoutError:
                    process.kill()
                    await process.communicate()
