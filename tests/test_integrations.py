"""Run with: uv run python -m unittest discover -s tests -v"""

import asyncio
import copy
import json
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import aiohttp
import httpx2
from aiohttp import web
from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull
from mcp.client.auth import OAuthFlowError
from mcp.client.auth.utils import validate_metadata_issuer
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from mcp.types import CallToolResult, TextContent
from pipecat.processors.aggregators.llm_context import LLMContext

from src.integrations.auth import AuthOutcome, AuthRequired, KeychainTokenStorage, MCPOAuthAdapter
from src.integrations.mcp import MCPIntegration, MCPRuntime
from src.integrations.spotify import SpotifyAuthAdapter


class MemoryStorage(KeychainTokenStorage):
    def __init__(self, url, record=None, *, interactive=False):
        super().__init__(url, interactive=interactive)
        self.record = record if record is not None else {}

    def _read(self):
        return copy.deepcopy(self.record)

    def _write(self, record):
        self.record.clear()
        self.record.update(copy.deepcopy(record))


class OAuthServer:
    def __init__(self):
        self.calls = []
        self.tokens = {}
        self.codes = {}
        self.refreshes = 0
        self.token_status = 200
        self.mcp_status = 200
        self.forbidden = False
        self.require_write = False
        self.deny = False
        self.public_discovery = False
        self.confidential_client = False
        self.registrations = 0
        self.authorization_requests = []
        self.token_requests = []

    async def __aenter__(self):
        app = web.Application()
        app.router.add_route("*", "/mcp", self.mcp)
        app.router.add_get("/.well-known/oauth-protected-resource/mcp", self.resource)
        app.router.add_get("/.well-known/oauth-authorization-server", self.metadata)
        app.router.add_post("/register", self.register)
        app.router.add_get("/authorize", self.authorize)
        app.router.add_post("/token", self.token)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.url = self.base + "/mcp"
        return self

    async def __aexit__(self, *args):
        await self.runner.cleanup()

    async def resource(self, request):
        return web.json_response({"resource": self.url, "authorization_servers": [self.base],
                                  "scopes_supported": ["read", "write"]})

    async def metadata(self, request):
        metadata = {
            "issuer": self.base, "authorization_endpoint": self.base + "/authorize",
            "token_endpoint": self.base + "/token", "registration_endpoint": self.base + "/register",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
        }
        if self.confidential_client:
            metadata.pop("registration_endpoint")
            metadata["token_endpoint_auth_methods_supported"] = ["client_secret_post"]
        return web.json_response(metadata)

    async def register(self, request):
        self.registrations += 1
        return web.json_response((await request.json()) | {"client_id": "test-client"}, status=201)

    async def authorize(self, request):
        self.authorization_requests.append(dict(request.query))
        code = f"code-{len(self.codes)}"
        self.codes[code] = request.query.get("scope", "read")
        params = {"state": request.query["state"]}
        params.update({"error": "access_denied"} if self.deny else {"code": code})
        raise web.HTTPFound(request.query["redirect_uri"] + "?" + urlencode(params))

    async def token(self, request):
        form = await request.post()
        self.token_requests.append(dict(form))
        if self.confidential_client and (form.get("client_id") != "registered-client"
                                          or form.get("client_secret") != "test-client-secret"):
            return web.json_response({"error": "invalid_client"}, status=401)
        if form["grant_type"] == "refresh_token":
            self.refreshes += 1
            if self.token_status != 200:
                return web.json_response({"error": "invalid_grant"}, status=self.token_status)
            scope = "read"
        else:
            self.assert_pkce = bool(form.get("code_verifier"))
            scope = self.codes[form["code"]]
        token = f"token-{len(self.tokens)}"
        self.tokens[token] = scope
        return web.json_response({"access_token": token, "token_type": "Bearer", "expires_in": 3600,
                                  "refresh_token": f"refresh-{len(self.tokens)}", "scope": scope})

    async def mcp(self, request):
        if self.mcp_status != 200:
            return web.Response(status=self.mcp_status)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        discovery = self.public_discovery and (request.method != "POST"
                                               or (await request.json())["method"] != "tools/call")
        if token not in self.tokens and not discovery:
            return web.json_response({"error": "unauthorized"}, status=401, headers={
                "WWW-Authenticate": f'Bearer resource_metadata="{self.base}/.well-known/oauth-protected-resource/mcp", scope="read"',
            })
        if request.method == "GET":
            return web.Response(status=405)
        if request.method == "DELETE":
            return web.Response(status=204)
        body = await request.json()
        method = body["method"]
        self.calls.append(method)
        if method == "tools/call" and self.forbidden:
            return web.Response(status=403)
        if method == "tools/call" and self.require_write and "write" not in self.tokens[token]:
            return web.Response(status=403, headers={
                "WWW-Authenticate": 'Bearer error="insufficient_scope", scope="write"',
            })
        if "id" not in body:
            return web.Response(status=202)
        if method == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "auth-test", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "read_value", "description": "Read the private test value",
                                  "inputSchema": {"type": "object", "properties": {}}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "The private value is 42."}]}
        else:
            return web.json_response({"jsonrpc": "2.0", "id": body["id"],
                                      "error": {"code": -32601, "message": "Unknown method"}})
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await OAuthServer().__aenter__()
        self.addAsyncCleanup(self.server.__aexit__)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.storage = MemoryStorage(self.server.url)
        self.adapter = MCPOAuthAdapter(self.server.url, f"http://127.0.0.1:{port}/callback", storage=self.storage)
        self.runtime = MCPRuntime([MCPIntegration("test", "Test private data", self.server.url, self.adapter)])
        self.addAsyncCleanup(self.runtime.close)
        self.browser_tasks = []
        loop = asyncio.get_running_loop()

        async def browse(url):
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    await response.read()

        def browser(url):
            self.browser_tasks.append(asyncio.run_coroutine_threadsafe(browse(url), loop))
            return True

        self.browser = patch("src.integrations.auth.webbrowser.open", browser)
        self.browser.start()
        self.addCleanup(self.browser.stop)

    async def login(self):
        initial = await self.runtime.connect("test")
        self.assertEqual(initial["status"], "auth_required")
        self.assertEqual(len(self.browser_tasks), 0)
        updates = []

        async def report(update):
            updates.append(update)

        result = await self.runtime.authorize("test", report)
        self.assertEqual(result["status"], "connected")
        self.assertEqual(updates[0]["status"], "awaiting_authorization")
        self.assertTrue(self.server.assert_pkce)

    async def test_login_discovers_tools_without_replaying_action(self):
        await self.login()
        self.assertNotIn("tools/call", self.server.calls)
        self.assertIn("test__read_value", self.runtime._catalog)
        result = await self.runtime.call_tool("test", "read_value", {})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.server.calls.count("tools/call"), 1)

    async def test_startup_keychain_approval_is_deferred_until_explicit_login(self):
        storage = KeychainTokenStorage(self.server.url)
        backing = self.storage

        def read():
            if not storage._interactive.get():
                raise AuthRequired("keychain_access_required")
            return backing._read()

        self.adapter = MCPOAuthAdapter(self.server.url, self.adapter.redirect_uri, storage=storage)
        self.runtime.integrations["test"].auth = self.adapter
        with patch.object(storage, "_read", side_effect=read) as reads, patch.object(
            storage, "_write", side_effect=backing._write,
        ):
            await self.runtime.__aenter__()
            self.assertIsNone(self.runtime._connections["test"].client)
            self.assertFalse(self.browser_tasks)
            self.assertEqual((await self.runtime.connect("test"))["reason"], "keychain_access_required")
            result = await self.runtime.authorize("test", lambda _: asyncio.sleep(0))
            self.assertEqual(result["status"], "connected")
            self.assertFalse(storage._interactive.get())
            self.assertEqual(reads.call_count, 3)
            self.assertEqual((await self.runtime.call_tool("test", "read_value", {}))["status"], "completed")
            self.assertEqual(reads.call_count, 3)

    async def test_registered_client_public_discovery_offline_scopes_and_restart(self):
        self.server.public_discovery = self.server.confidential_client = True
        await self.storage.set_client_info(OAuthClientInformationFull(
            client_id="registered-client", client_secret="test-client-secret",
            token_endpoint_auth_method="client_secret_post", issuer=self.server.base,
        ))
        self.adapter = MCPOAuthAdapter(
            self.server.url, self.adapter.redirect_uri, scopes="read", storage=self.storage,
            authorization_params={"access_type": "offline", "prompt": "consent"},
        )
        self.runtime.integrations["test"].auth = self.adapter
        self.assertEqual((await self.runtime.connect("test"))["status"], "connected")
        self.assertFalse(self.browser_tasks)
        updates = []

        async def report(update):
            updates.append(update)

        self.assertEqual((await self.runtime.authorize("test", report))["status"], "connected")
        self.assertNotIn("tools/call", self.server.calls)
        query = self.server.authorization_requests[0]
        self.assertEqual(query["access_type"], "offline")
        self.assertEqual(query["prompt"], "consent")
        self.assertEqual(query["scope"], "read")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(self.server.token_requests[0]["client_secret"], "test-client-secret")
        self.assertEqual(self.server.registrations, 0)
        self.assertEqual(updates[0]["status"], "awaiting_authorization")
        self.server.require_write = True
        self.assertEqual((await self.runtime.call_tool("test", "read_value", {}))["status"], "auth_required")
        calls = self.server.calls.count("tools/call")
        self.assertEqual((await self.runtime.authorize("test", report))["status"], "connected")
        self.assertEqual(self.server.calls.count("tools/call"), calls)
        self.assertEqual(set(self.server.authorization_requests[-1]["scope"].split()), {"read", "write"})
        await self.runtime.close()
        self.storage.record["expires_at"] = time.time() - 10
        adapter = MCPOAuthAdapter(self.server.url, self.adapter.redirect_uri,
                                 storage=MemoryStorage(self.server.url, self.storage.record))
        runtime = MCPRuntime([MCPIntegration("test", "Test", self.server.url, adapter)])
        self.addAsyncCleanup(runtime.close)
        self.assertEqual((await runtime.connect("test"))["status"], "connected")
        self.assertEqual(self.server.refreshes, 1)
        self.assertEqual(len(self.browser_tasks), 2)
        self.assertEqual(self.server.token_requests[-1]["grant_type"], "refresh_token")
        self.assertEqual(self.server.token_requests[-1]["client_secret"], "test-client-secret")

    async def test_authorization_options_cannot_override_security_fields(self):
        for key in ("state", "client_id", "redirect_uri", "scope", "code_challenge", "resource",
                    "client_secret", "access_token", "refresh_token", "id_token", "code", "", " "):
            with self.assertRaises(ValueError):
                MCPOAuthAdapter(self.server.url, authorization_params={key: "overridden"})
        with self.assertRaises(ValueError):
            MCPOAuthAdapter(self.server.url, authorization_params={"access_type": 1})

    async def test_google_metadata_compatibility_preserves_issuer_validation(self):
        metadata = {"resource": "https://gmailmcp.googleapis.com/mcp/v1",
                    "authorization_servers": ["https://accounts.google.com/"]}
        request = httpx2.Request("GET", "https://gmailmcp.googleapis.com/.well-known/oauth-protected-resource/mcp/v1")
        response = await self.adapter._check_oauth_response(request, httpx2.Response(200, json=metadata, request=request))
        resource = ProtectedResourceMetadata.model_validate(response.json())
        issuer = OAuthMetadata(issuer="https://accounts.google.com",
                               authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
                               token_endpoint="https://oauth2.googleapis.com/token")
        validate_metadata_issuer(issuer, str(resource.authorization_servers[0]))
        request = httpx2.Request("GET", self.server.base + "/.well-known/oauth-protected-resource/mcp")
        response = await self.adapter._check_oauth_response(request, httpx2.Response(200, json=metadata, request=request))
        with self.assertRaises(OAuthFlowError):
            validate_metadata_issuer(issuer, response.json()["authorization_servers"][0])

    async def test_restart_refreshes_and_rotates_without_browser(self):
        await self.login()
        await self.runtime.close()
        self.storage.record["expires_at"] = time.time() - 10
        adapter = MCPOAuthAdapter(self.server.url, self.adapter.redirect_uri,
                                 storage=MemoryStorage(self.server.url, self.storage.record))
        runtime = MCPRuntime([MCPIntegration("test", "Test", self.server.url, adapter)])
        self.addAsyncCleanup(runtime.close)
        result = await runtime.connect("test")
        self.assertEqual(result["status"], "connected")
        self.assertEqual(self.server.refreshes, 1)
        self.assertEqual(len(self.browser_tasks), 1)
        self.assertEqual(self.storage.record["tokens"]["refresh_token"], "refresh-2")

    async def test_revoked_refresh_requires_auth_without_browser(self):
        await self.login()
        self.adapter.provider.context.token_expiry_time = time.time() - 10
        self.server.token_status = 400
        result = await self.runtime.connect("test")
        self.assertEqual(result["status"], "auth_required")
        self.assertIsNone(self.storage.record["tokens"])
        self.assertEqual(len(self.browser_tasks), 1)

    async def test_transient_refresh_failure_does_not_launch_login(self):
        await self.login()
        self.adapter.provider.context.token_expiry_time = time.time() - 10
        self.server.token_status = 503
        result = await self.runtime.connect("test")
        self.assertEqual(result["status"], "failed")
        self.assertIsNotNone(self.storage.record["tokens"])
        self.assertEqual(len(self.browser_tasks), 1)

    async def test_auth_notice_does_not_misclassify_later_outage(self):
        self.assertEqual((await self.runtime.connect("test"))["status"], "auth_required")
        self.server.mcp_status = 503
        self.assertEqual((await self.runtime.connect("test"))["status"], "failed")
        self.assertFalse(self.browser_tasks)

    async def test_scope_step_up_only_replays_discovery(self):
        await self.login()
        self.server.require_write = True
        result = await self.runtime.call_tool("test", "read_value", {})
        self.assertEqual(result["reason"], "additional_scope_required")
        count = self.server.calls.count("tools/call")

        async def report(update):
            pass

        result = await self.runtime.authorize("test", report)
        self.assertEqual(result["status"], "connected")
        self.assertEqual(self.server.calls.count("tools/call"), count)
        self.assertIn("write", self.storage.record["tokens"]["scope"])
        result = await self.runtime.call_tool("test", "read_value", {})
        self.assertEqual(result["status"], "completed")

    async def test_ordinary_forbidden_is_not_retried_or_reauthorized(self):
        await self.login()
        self.server.forbidden = True
        result = await self.runtime.call_tool("test", "read_value", {})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.server.calls.count("tools/call"), 1)
        self.assertEqual(len(self.browser_tasks), 1)

    async def test_denial_closes_callback(self):
        self.server.deny = True
        await self.runtime.connect("test")

        async def report(update):
            pass

        result = await self.runtime.authorize("test", report)
        self.assertEqual(result["status"], "denied")
        self.assertFalse(self.runtime._authorizing)

    async def test_stdio_transport_and_tool_isolation(self):
        params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).resolve()), "--server"],
                                       env={"PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        runtime = MCPRuntime([MCPIntegration("one", "Echo", params, tools=["echo"]),
                              MCPIntegration("two", "Echo", params)])
        async with runtime:
            schema = runtime.tools_schema()
            names = [t.name for t in schema.standard_tools]
            self.assertIn("one__echo", names)
            self.assertIn("two__echo", names)
            self.assertNotIn("search_tools", names)
            self.assertEqual((await runtime.call_tool("one", "echo", {"value": 7}))["status"], "completed")
            self.assertEqual((await runtime.call_tool("one", "unknown", {}))["reason"], "unknown_tool")
            self.assertEqual((await runtime.call_tool("missing", "echo", {}))["reason"], "unknown_integration")
        self.assertTrue(all(c.client is None for c in runtime._connections.values()))

    async def test_duplicate_timeout_cancellation_and_progress(self):
        started, finish = asyncio.Event(), asyncio.Event()

        class WaitingAuth:
            http_auth = None

            def classify_failure(self, failure):
                return None

            async def authorize(self, report):
                await report({"status": "awaiting_authorization"})
                started.set()
                await finish.wait()
                return AuthOutcome("denied")

        runtime = MCPRuntime([MCPIntegration("wait", "Wait", self.server.url, WaitingAuth())])
        self.addAsyncCleanup(runtime.close)
        updates = []

        async def report(update):
            updates.append(update)

        first = asyncio.create_task(runtime.authorize("wait", report))
        await started.wait()
        duplicate = await runtime.authorize("wait", report)
        self.assertEqual(duplicate["status"], "awaiting_authorization")
        self.assertEqual(len(updates), 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(runtime._authorizing)
        with patch("src.integrations.mcp.AUTH_TIMEOUT", 0.01):
            self.assertEqual((await runtime.authorize("wait", report))["status"], "timed_out")
        first = asyncio.create_task(runtime.authorize("wait", report))
        await asyncio.sleep(0.01)
        await runtime.close()
        self.assertTrue(first.cancelled())

    async def test_pipecat_handler_publishes_schemas_before_completion(self):
        context = LLMContext()
        results = []

        async def receive(result, properties=None):
            results.append((result, properties))
            if result["status"] == "connected":
                self.assertIn("test__read_value", [t.name for t in self.runtime.tools_schema().standard_tools])

        await self.runtime.connect("test")
        params = SimpleNamespace(arguments={"integration_id": "test"}, context=context, result_callback=receive)
        await self.runtime._authorize_tool(params)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][1].is_final)
        self.assertTrue(results[-1][1].run_llm)
        self.assertEqual(results[-1][0]["status"], "connected")
        options = self.runtime._authorize_tool
        self.assertFalse(options._pipecat_cancel_on_interruption)
        self.assertTrue(options._pipecat_cancellable_by_llm)

    async def test_callback_rejects_wrong_state(self):
        from src.integrations.auth import _Callback
        async with _Callback(self.adapter.redirect_uri) as callback:
            callback.state = "expected"
            async with aiohttp.ClientSession() as session:
                async with session.get(callback.uri, params={"state": "wrong", "code": "secret-code"}) as response:
                    self.assertEqual(response.status, 400)
            self.assertFalse(callback.result.done())

    async def test_authorization_timeout_closes_listener(self):
        await self.runtime.connect("test")

        async def report(update):
            pass

        with patch("src.integrations.auth.webbrowser.open", return_value=True), \
                patch("src.integrations.mcp.AUTH_TIMEOUT", 0.1):
            result = await self.runtime.authorize("test", report)
        self.assertEqual(result["status"], "timed_out")
        self.assertFalse(self.runtime._authorizing)
        async with aiohttp.ClientSession() as session:
            with self.assertRaises(aiohttp.ClientConnectorError):
                await session.get(self.adapter.redirect_uri, params={"state": "late", "code": "late"})

    async def test_malformed_config_does_not_stop_conversation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mcp.toml"
            for content in ('integrations = []', '[integrations]\nbad = 7',
                            '[integrations.bad]\nurl = 1', '[integrations.bad]\ncommand = 2'):
                path.write_text(content)
                self.assertFalse(MCPRuntime.from_config(path).integrations)

    async def test_spotify_adapter_classifies_only_auth_and_cleans_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "spotify-config.json"
            config = {"clientId": "id", "clientSecret": "secret", "redirectUri": self.adapter.redirect_uri,
                      "accessToken": "access", "refreshToken": "refresh", "expiresAt": 1}
            path.write_text(json.dumps(config))
            adapter = SpotifyAuthAdapter(directory, sys.executable)
            forbidden = CallToolResult(content=[TextContent(type="text", text="Error: Spotify API failed (403): Premium required")])
            self.assertIsNone(adapter.classify_failure(forbidden))
            self.assertTrue(forbidden.is_error)
            success = CallToolResult(content=[TextContent(type="text", text='Playlist: "Error 401"')])
            self.assertIsNone(adapter.classify_failure(success))
            temporary_failure = CallToolResult(content=[TextContent(type="text", text='Error: Spotify API failed (503)')])
            self.assertIsNone(adapter.classify_failure(temporary_failure))
            config.pop("accessToken")
            path.write_text(json.dumps(config))
            self.assertIsInstance(adapter.classify_failure(forbidden), AuthRequired)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            (directory / "build").mkdir()
            (directory / "build/auth.js").write_text("import time\ntime.sleep(60)\n")
            started = asyncio.Event()

            async def report(update):
                started.set()

            task = asyncio.create_task(adapter.authorize(report))
            await started.wait()
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


def run_stdio_fixture():
    server = MCPServer("stdio-fixture")

    @server.tool()
    def echo(value: int) -> str:
        """Echo an integer."""
        return str(value)

    server.run()


if __name__ == "__main__":
    if "--server" in sys.argv:
        run_stdio_fixture()
    else:
        unittest.main()
