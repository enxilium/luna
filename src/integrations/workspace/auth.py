"""Public Google OAuth, reusing Luna's service-bound Keychain and callback."""

import asyncio
import time
import webbrowser
from datetime import datetime, timezone

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from mcp.shared.auth import OAuthToken
from mcp.types import CallToolResult

from src.integrations.auth import (
    AuthDenied,
    AuthFailure,
    AuthOutcome,
    AuthRequired,
    KeychainTokenStorage,
    _Callback,
)

from . import SCOPE_BASE, SERVICES, WRITE_SCOPES, credential_resource


class GoogleAPIAuthAdapter:
    http_auth = None

    def __init__(
        self,
        service,
        *,
        redirect_uri="http://127.0.0.1:8765/oauth/callback",
        scopes=None,
        storage=None,
    ):
        if service not in SERVICES:
            raise ValueError("Unknown Workspace service")
        if scopes is not None and not isinstance(scopes, str):
            raise ValueError("Workspace scopes must be space-separated text")
        _Callback(redirect_uri)
        self.service, self.redirect_uri = service, redirect_uri
        self.scopes = set(
            (scopes or " ".join(SCOPE_BASE + s for s in SERVICES[service][2])).split()
        )
        self.storage = storage or KeychainTokenStorage(credential_resource(service))
        self._required_scopes = set()
        self._lock = asyncio.Lock()

    def classify_failure(self, failure):
        if isinstance(failure, AuthRequired):
            return failure
        if isinstance(failure, CallToolResult):
            data = failure.structured_content or {}
            if data.get("status") == "auth_required":
                allowed = {SCOPE_BASE + s for s in WRITE_SCOPES[self.service]}
                self._required_scopes.update(
                    set(data.get("required_scopes", [])) & allowed
                )
                return AuthRequired(data.get("reason", "sign_in_required"))
        return None

    async def _credentials(self):
        token = await self.storage.get_tokens()
        client = await self.storage.get_client_info()
        if not token or not client:
            raise AuthRequired("sign_in_required")
        expiry = await self.storage.get_token_expiry()
        return Credentials(
            token=token.access_token,
            refresh_token=token.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client.client_id,
            client_secret=client.client_secret,
            scopes=token.scope.split() if token.scope else None,
            expiry=datetime.fromtimestamp(expiry, timezone.utc).replace(tzinfo=None)
            if expiry
            else datetime.min,
        )

    async def access_token(self):
        async with self._lock:
            credentials = await self._credentials()
            if not credentials.valid:
                if not credentials.refresh_token:
                    raise AuthRequired("sign_in_required")
                try:
                    await asyncio.to_thread(credentials.refresh, Request())
                except RefreshError as error:
                    # Temporary refresh faults must not destroy the existing grant.
                    if not error.retryable and "invalid_grant" in str(error):
                        await self.storage.clear_tokens()
                        raise AuthRequired("sign_in_required") from None
                    raise AuthFailure("token_refresh_failed") from None
                await self._save(credentials)
            return credentials.token

    async def _save(self, credentials):
        expiry = (
            credentials.expiry.replace(tzinfo=timezone.utc).timestamp()
            if credentials.expiry
            else time.time()
        )
        await self.storage.set_tokens(
            OAuthToken(
                access_token=credentials.token,
                token_type="Bearer",
                refresh_token=credentials.refresh_token,
                scope=" ".join(credentials.granted_scopes or credentials.scopes or []),
                expires_in=max(0, int(expiry - time.time())),
            )
        )

    async def authorize(self, report_progress):
        interaction = self.storage._interactive.set(True)
        try:
            client = await self.storage.get_client_info()
            if not client or not client.client_secret:
                return AuthOutcome("failed", "google_client_not_configured")
            previous = await self.storage.get_tokens()
            scopes = self.scopes | self._required_scopes
            if previous and previous.scope:
                scopes.update(previous.scope.split())
            config = {
                "web": {
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [self.redirect_uri],
                }
            }
            flow = Flow.from_client_config(
                config, scopes=sorted(scopes), autogenerate_code_verifier=True
            )
            flow.redirect_uri = self.redirect_uri
            async with _Callback(self.redirect_uri) as callback:
                url, callback.state = flow.authorization_url(
                    access_type="offline", prompt="consent"
                )
                await report_progress({"status": "awaiting_authorization"})
                if not await asyncio.to_thread(webbrowser.open, url):
                    return AuthOutcome("failed", "browser_unavailable")
                result = await callback.result
                await asyncio.to_thread(flow.fetch_token, code=result.code, timeout=30)
                credentials = flow.credentials
                if not credentials.refresh_token and previous:
                    credentials._refresh_token = previous.refresh_token
                await self._save(credentials)
                self._required_scopes.clear()
                return AuthOutcome("connected")
        except AuthDenied:
            return AuthOutcome("denied")
        except Exception:
            return AuthOutcome("failed", "authorization_failed")
        finally:
            self.storage._interactive.reset(interaction)
