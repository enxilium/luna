"""Compatibility with the unchanged Spotify MCP server's local OAuth script."""

import asyncio
import json
import os
import re
import time
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from src.integrations.auth import AuthFailure, AuthOutcome, AuthRequired, Progress


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
            isinstance(config.get(k), str) and config[k] for k in ("clientId", "clientSecret", "redirectUri")
        ):
            raise AuthFailure("spotify_configuration_invalid")
        return config

    def classify_failure(self, failure) -> AuthRequired | None:
        if isinstance(failure, AuthRequired):
            return failure
        if isinstance(failure, CallToolResult):
            errors = [c.text for c in failure.content if isinstance(c, TextContent)
                      and (failure.is_error or c.text.lstrip().lower().startswith("error"))]
            if not errors:
                return None
            failure.is_error = True  # Some upstream handlers put errors only in text.
            message = " ".join(errors)
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
        if any(s in message for s in ("insufficient client scope", "insufficient_scope")):
            return AuthRequired("additional_scope_required")
        if re.search(r"\b401\b", message) or "valid user authentication required" in message:
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
        if redirect.scheme != "http" or redirect.hostname != "127.0.0.1" or not redirect.port:
            return AuthOutcome("failed", "spotify_callback_must_use_loopback_ip")
        script = self.directory / "build/auth.js"
        if not script.is_file():
            return AuthOutcome("failed", "spotify_server_not_built")
        await report_progress({"status": "awaiting_authorization"})
        try:
            process = await asyncio.create_subprocess_exec(
                self.node, str(script), cwd=self.directory,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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
            if (not all(isinstance(config.get(k), str) and config[k] for k in ("accessToken", "refreshToken"))
                    or not isinstance(config.get("expiresAt"), (int, float))
                    or not config["expiresAt"] > time.time() * 1000):
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
