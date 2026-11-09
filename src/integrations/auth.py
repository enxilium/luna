"""MCP OAuth using the SDK, a loopback callback, and macOS Keychain."""

import asyncio
import contextvars
import hashlib
import json
import logging
import secrets
import threading
import time
import webbrowser
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
from aiohttp import web
from mcp import Client
from mcp.client.auth.oauth2 import OAuthClientProvider, OAuthContext
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from mcp.types import CallToolResult


Progress = Callable[[dict], Awaitable[None]]
_KEYCHAIN_LOCK = threading.Lock()


class AuthRequired(Exception):
    def __init__(self, reason: str = "sign_in_required"):
        super().__init__(reason)
        self.reason = reason


class AuthDenied(Exception):
    pass


class AuthFailure(Exception):
    """A safe diagnostic; never include provider responses or credentials."""


@dataclass(frozen=True)
class AuthOutcome:
    status: str
    reason: str | None = None

    def as_dict(self, integration_id: str) -> dict:
        result = {"status": self.status, "integration": integration_id}
        if self.reason:
            result["reason"] = self.reason
        return result


class AuthAdapter(Protocol):
    @property
    def http_auth(self) -> httpx2.Auth | None: ...

    def classify_failure(self, failure: Exception | CallToolResult) -> AuthRequired | None: ...

    async def authorize(self, report_progress: Progress) -> AuthOutcome: ...


def validate_http_url(url: str) -> None:
    parsed = urlsplit(url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Expected an HTTP URL without credentials or a fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    ):
        raise ValueError("HTTP is allowed only on loopback; remote services require HTTPS")


class _SafeOAuthLogs(logging.Filter):
    def filter(self, record):
        if record.exc_info:
            record.msg, record.args = "MCP authorization failed", ()
            record.exc_info = record.exc_text = None
        return True


logging.getLogger("mcp.client.auth.oauth2").addFilter(_SafeOAuthLogs())


class KeychainTokenStorage:
    """The SDK's TokenStorage contract, with absolute expiry and issuer metadata."""

    def __init__(self, server_url: str, client_id: str | None = None, *, interactive: bool = False):
        self.server_url = server_url
        self.client_id = client_id
        self.context: OAuthContext | None = None
        self._lock = asyncio.Lock()
        self._record: dict | None = None
        self._interactive = contextvars.ContextVar("keychain_interaction", default=interactive)
        self._account = hashlib.sha256(server_url.encode()).hexdigest()

    def _query(self) -> dict:
        import Security as S

        return {
            S.kSecClass: S.kSecClassGenericPassword,
            S.kSecAttrService: "Luna MCP",
            S.kSecAttrAccount: self._account,
        }

    @contextmanager
    def _access(self):
        import Security as S

        # Login-Keychain interaction is process-wide; per-query UI flags do not suppress its dialogs.
        with _KEYCHAIN_LOCK:
            status, allowed = S.SecKeychainGetUserInteractionAllowed(None)
            if status != S.errSecSuccess:
                raise AuthFailure("keychain_unavailable")
            if S.SecKeychainSetUserInteractionAllowed(self._interactive.get()) != S.errSecSuccess:
                raise AuthFailure("keychain_unavailable")
            try:
                yield
            finally:
                if S.SecKeychainSetUserInteractionAllowed(allowed) != S.errSecSuccess:
                    raise AuthFailure("keychain_unavailable")

    def _read(self) -> dict:
        import Security as S

        with self._access():
            status, data = S.SecItemCopyMatching(
                self._query() | {S.kSecReturnData: True, S.kSecMatchLimit: S.kSecMatchLimitOne}, None
            )
        if status == S.errSecItemNotFound:
            return {}
        self._check_status(status)
        record = json.loads(bytes(data))
        if record.get("server_url") != self.server_url:
            raise AuthFailure("credential_binding_mismatch")
        return record

    def _write(self, record: dict) -> None:
        import Security as S
        from Foundation import NSData

        payload = json.dumps(record | {"server_url": self.server_url}).encode()
        data = NSData.dataWithBytes_length_(payload, len(payload))
        with self._access():
            status = S.SecItemUpdate(self._query(), {S.kSecValueData: data})
            if status == S.errSecItemNotFound:
                status, _ = S.SecItemAdd(self._query() | {S.kSecValueData: data}, None)
        self._check_status(status)

    def _check_status(self, status: int) -> None:
        import Security as S

        if status == S.errSecInteractionNotAllowed or (
            status == S.errSecAuthFailed and not self._interactive.get()
        ):
            raise AuthRequired("keychain_access_required")
        if status != S.errSecSuccess:
            raise AuthFailure("keychain_unavailable")

    async def _load(self) -> dict:
        async with self._lock:
            if self._record is None:
                self._record = await asyncio.to_thread(self._read)
            return self._record

    async def _update(self, values: dict) -> None:
        async with self._lock:
            record = self._record if self._record is not None else await asyncio.to_thread(self._read)
            updated = record | values
            await asyncio.to_thread(self._write, updated)
            self._record = updated

    def _metadata(self) -> dict:
        context = self.context
        if context is None:
            return {}
        return {
            "authorization_server": context.auth_server_url,
            "oauth_metadata": context.oauth_metadata.model_dump(mode="json")
            if context.oauth_metadata else None,
            "resource_metadata": context.protected_resource_metadata.model_dump(mode="json")
            if context.protected_resource_metadata else None,
        }

    async def save_metadata(self) -> None:
        await self._update(self._metadata())

    async def get_tokens(self) -> OAuthToken | None:
        record = await self._load()
        if self.context is not None:
            self.context.token_expiry_time = record.get("expires_at")
            self.context.auth_server_url = record.get("authorization_server")
            if record.get("oauth_metadata"):
                self.context.oauth_metadata = OAuthMetadata.model_validate(record["oauth_metadata"])
            if record.get("resource_metadata"):
                self.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(
                    record["resource_metadata"]
                )
        return OAuthToken.model_validate(record["tokens"]) if record.get("tokens") else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        await self._update(self._metadata() | {
            "tokens": tokens.model_dump(mode="json"),
            "expires_at": time.time() + tokens.expires_in if tokens.expires_in is not None else None,
        })

    async def get_token_expiry(self) -> float | None:
        return (await self._load()).get("expires_at")

    async def clear_tokens(self) -> None:
        await self._update({"tokens": None, "expires_at": None})

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        record = await self._load()
        if record.get("client_info"):
            return OAuthClientInformationFull.model_validate(record["client_info"])
        if self.client_id:
            return OAuthClientInformationFull(client_id=self.client_id, token_endpoint_auth_method="none")
        return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._update({"client_info": client_info.model_dump(mode="json")})


class _Callback:
    def __init__(self, redirect_uri: str):
        parsed = urlsplit(redirect_uri)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError("OAuth callbacks must use an HTTP loopback IP address")
        if not parsed.port or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("OAuth callbacks require a port and no query, fragment, or credentials")
        self.uri, self.parsed = redirect_uri, parsed
        self.state: str | None = None
        self.result: asyncio.Future | None = None
        self.runner: web.AppRunner | None = None

    async def __aenter__(self):
        self.result = asyncio.get_running_loop().create_future()
        app = web.Application()
        app.router.add_get(self.parsed.path or "/", self._receive)
        self.runner = web.AppRunner(app, access_log=None)
        try:
            await self.runner.setup()
            await web.TCPSite(self.runner, self.parsed.hostname, self.parsed.port).start()
        except BaseException:
            await self.runner.cleanup()
            raise
        return self

    async def __aexit__(self, *args):
        if self.result and not self.result.done():
            self.result.cancel()
        await self.runner.cleanup()

    async def _receive(self, request):
        state = request.query.get("state", "")
        if not self.state or not secrets.compare_digest(state, self.state):
            return web.Response(status=400, text="Invalid authorization state.")
        if self.result.done():
            return web.Response(status=409, text="This authorization has already finished.")
        if request.query.get("error"):
            self.result.set_exception(AuthDenied())
            return web.Response(text="Authorization declined. You can return to Luna.")
        if not request.query.get("code"):
            return web.Response(status=400, text="Missing authorization code.")
        self.result.set_result(AuthorizationCodeResult(
            code=request.query["code"], state=state, iss=request.query.get("iss")
        ))
        return web.Response(text="Authorization received. You can return to Luna.")


class _OAuthHTTPAuth(httpx2.Auth):
    requires_response_body = True

    def __init__(self, adapter):
        self.adapter = adapter

    async def async_auth_flow(self, request):
        adapter, refreshed = self.adapter, False
        pending = adapter._challenge if adapter._interactive.get() else None
        while True:
            flow = adapter.provider.async_auth_flow(request)
            retry = False
            try:
                outgoing = await anext(flow)
                while True:
                    # Reuse a real challenge on a harmless discovery request, never the failed action.
                    if (outgoing is request and adapter._interactive.get() and not pending
                            and not adapter.provider.context.is_token_valid()):
                        pending = (401, {})  # Discovery can be public even when service tools need OAuth.
                    if outgoing is request and pending:
                        status, headers = pending
                        pending = None
                        response = httpx2.Response(status, headers=headers, request=request)
                    else:
                        if outgoing is not request:
                            validate_http_url(str(outgoing.url))
                        response = yield outgoing
                    if outgoing is request:
                        challenge = response.headers.get("WWW-Authenticate", "")
                        needs_scope = response.status_code == 403 and 'insufficient_scope' in challenge
                        if response.status_code == 401 or needs_scope:
                            adapter._challenge = (response.status_code, {"WWW-Authenticate": challenge})
                            context = adapter.provider.context
                            if response.status_code == 401 and context.can_refresh_token() and not refreshed:
                                context.token_expiry_time = time.time() - 1
                                refreshed, retry = True, True
                                break
                        elif response.status_code == 403:
                            return  # The SDK otherwise retries ordinary forbidden requests.
                        elif response.is_success:
                            adapter._deferred = adapter._challenge = None
                    else:
                        response = await adapter._check_oauth_response(outgoing, response)
                    outgoing = await flow.asend(response)
            except StopAsyncIteration:
                return
            finally:
                await flow.aclose()
            if not retry:
                return


class MCPOAuthAdapter:
    def __init__(
        self,
        server_url: str,
        redirect_uri: str = "http://127.0.0.1:8765/oauth/callback",
        client_id: str | None = None,
        scopes: str | None = None,
        storage: KeychainTokenStorage | None = None,
        authorization_params: dict[str, str] | None = None,
    ):
        validate_http_url(server_url)
        _Callback(redirect_uri)
        reserved = {"client_id", "redirect_uri", "response_type", "state", "scope", "resource",
                    "code_challenge", "code_challenge_method", "nonce", "request", "request_uri",
                    "client_secret", "access_token", "refresh_token", "id_token", "code"}
        if authorization_params is not None and (
            not isinstance(authorization_params, dict)
            or any(not isinstance(k, str) or not k.strip() or not isinstance(v, str) or k in reserved
                   for k, v in authorization_params.items())
        ):
            raise ValueError("Authorization options must be text and cannot replace OAuth security fields")
        if scopes is not None and (not isinstance(scopes, str) or not scopes.strip()):
            raise ValueError("scopes must be nonempty text")
        self.scopes = scopes
        self.authorization_params = dict(authorization_params or {})
        self.server_url, self.redirect_uri = server_url, redirect_uri
        self.storage = storage or KeychainTokenStorage(server_url, client_id)
        self._interactive = contextvars.ContextVar("mcp_authorization", default=None)
        self._challenge = None
        self._deferred: AuthRequired | None = None
        self.provider = OAuthClientProvider(
            server_url=server_url,
            client_metadata=OAuthClientMetadata(
                client_name="Luna", redirect_uris=[redirect_uri],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"], token_endpoint_auth_method="none", scope=scopes,
            ),
            storage=self.storage,
            redirect_handler=self._redirect,
            callback_handler=self._callback,
        )
        self.storage.context = self.provider.context
        self.http_auth = _OAuthHTTPAuth(self)

    def classify_failure(self, failure) -> AuthRequired | None:
        if isinstance(failure, AuthRequired):
            self._deferred = None
            return failure
        if not isinstance(failure, CallToolResult):
            # The SDK can turn transport exceptions into a generic connection-closed error.
            required, self._deferred = self._deferred, None
            return required
        return None

    async def _check_oauth_response(self, request, response):
        if response.status_code == 429 or response.status_code >= 500:
            raise AuthFailure("authorization_service_unavailable")
        # Google PRM publishes a trailing slash absent from its canonical issuer.
        # Normalize only this known provider mismatch; the SDK still checks issuers exactly.
        if (response.is_success and request.url.host.endswith(".googleapis.com")
                and request.url.path.startswith("/.well-known/oauth-protected-resource/")):
            await response.aread()
            data = response.json()
            if data.get("authorization_servers") == ["https://accounts.google.com/"]:
                data["authorization_servers"] = ["https://accounts.google.com"]
                response = httpx2.Response(response.status_code, json=data, request=request)
        metadata = self.provider.context.oauth_metadata
        if metadata and str(request.url) == str(metadata.token_endpoint):
            await response.aread()
            if response.status_code >= 400:
                try:
                    error = response.json().get("error")
                except (ValueError, AttributeError):
                    error = None
                if error == "invalid_grant" and b"grant_type=refresh_token" in request.content:
                    await self.storage.clear_tokens()
                    return response
                raise AuthFailure("token_exchange_failed")
            try:
                OAuthToken.model_validate(response.json())
            except Exception:
                raise AuthFailure("invalid_token_response") from None
        return response

    async def _redirect(self, url):
        await self.storage.save_metadata()
        interactive = self._interactive.get()
        if interactive is None:
            self._deferred = AuthRequired(
                "additional_scope_required" if self._challenge and self._challenge[0] == 403 else "sign_in_required"
            )
            raise self._deferred
        callback, report = interactive
        validate_http_url(url)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if self.scopes and (not self._challenge or self._challenge[0] != 403):
            # Explicit initial scopes take precedence over the SDK's all-supported-scopes fallback.
            query["scope"] = [self.scopes]
            self.provider.context.client_metadata.scope = self.scopes
        query.update({k: [v] for k, v in self.authorization_params.items()})
        url = parsed._replace(query=urlencode(query, doseq=True)).geturl()
        callback.state = query.get("state", [None])[0]
        if not callback.state:
            raise AuthFailure("invalid_authorization_url")
        await report({"status": "awaiting_authorization"})
        if not await asyncio.to_thread(webbrowser.open, url):
            raise AuthFailure("browser_unavailable")

    async def _callback(self):
        callback, _ = self._interactive.get()
        return await callback.result

    async def authorize(self, report_progress: Progress) -> AuthOutcome:
        interaction = self.storage._interactive.set(True)
        try:
            async with _Callback(self.redirect_uri) as callback:
                token = self._interactive.set((callback, report_progress))
                try:
                    async with httpx2.AsyncClient(auth=self.http_auth, timeout=330, follow_redirects=False) as http:
                        transport = streamable_http_client(self.server_url, http_client=http)
                        async with Client(transport, cache=None, read_timeout_seconds=330) as client:
                            await client.list_tools()
                    self._deferred = self._challenge = None
                    return AuthOutcome("connected")
                finally:
                    self._interactive.reset(token)
        except BaseExceptionGroup as group:
            if group.subgroup(AuthDenied):
                return AuthOutcome("denied")
            if group.subgroup(AuthFailure):
                return AuthOutcome("failed", "authorization_failed")
            raise
        except AuthDenied:
            return AuthOutcome("denied")
        except (AuthFailure, OSError):
            return AuthOutcome("failed", "authorization_failed")
        finally:
            self.storage._interactive.reset(interaction)
