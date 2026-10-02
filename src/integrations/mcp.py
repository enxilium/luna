"""Shared MCP runtime: connection lifetime, schemas, tool execution, and auth results."""

import asyncio
import hashlib
import os
import re
import tomllib
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx2
from loguru import logger
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, InputRequiredResult, TextContent, Tool
from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import FunctionCallResultProperties
from pipecat.services.llm_service import FunctionCallParams

from src.integrations.auth import AuthAdapter, AuthFailure, AuthOutcome, AuthRequired, MCPOAuthAdapter
from src.integrations.spotify import SpotifyAuthAdapter


CONFIG_PATH = Path.home() / "Library/Application Support/Luna/mcp.toml"
AUTH_TIMEOUT = 295.0  # Leave five seconds for cleanup before Pipecat's 300-second deadline.


@dataclass
class MCPIntegration:
    integration_id: str
    description: str
    server: str | StdioServerParameters
    auth: AuthAdapter | None = None
    tools: list[str] | None = None


class _Connection:
    def __init__(self, integration: MCPIntegration):
        self.integration = integration
        self.client: Client | None = None
        self.error: BaseException | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> Client:
        async with self._lock:
            if self.client is not None:
                return self.client
            if self._task:
                await self._task
            self.error = None
            self._stop = asyncio.Event()
            ready = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(self._own(ready), name=f"mcp:{self.integration.integration_id}")
            try:
                return await asyncio.shield(ready)
            except asyncio.CancelledError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
                raise

    async def _own(self, ready):
        try:
            async with AsyncExitStack() as stack:
                server, auth = self.integration.server, self.integration.auth
                if isinstance(server, str):
                    http = await stack.enter_async_context(httpx2.AsyncClient(
                        auth=auth.http_auth if auth else None, timeout=30, follow_redirects=False,
                    ))
                    server = streamable_http_client(server, http_client=http)
                else:
                    errlog = stack.enter_context(open(os.devnull, "w"))
                    server = stdio_client(server, errlog=errlog)
                self.client = await stack.enter_async_context(Client(server, cache=None, read_timeout_seconds=30))
                ready.set_result(self.client)
                await self._stop.wait()
        except BaseException as error:
            self.error = error
            if not ready.done():
                ready.set_exception(error)
                ready.exception()
        finally:
            self.client = None

    async def close(self):
        async with self._lock:
            if self._task:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
                self._task = None
            self.client = None


class _MCPSchema(FunctionSchema):
    """Preserve the complete MCP JSON schema, including definitions and constraints."""

    def __init__(self, name, tool: Tool, handler):
        self.input_schema = tool.input_schema
        super().__init__(name, tool.description or tool.name,
                         tool.input_schema.get("properties", {}), tool.input_schema.get("required", []), handler)

    def to_default_dict(self):
        return super().to_default_dict() | {"parameters": self.input_schema}


def _causes(error):
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            yield from _causes(child)
    else:
        yield error


class MCPRuntime:
    def __init__(self, integrations: list[MCPIntegration]):
        ids = [i.integration_id for i in integrations]
        if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", i) for i in ids):
            raise ValueError("Integration IDs must be unique lowercase names of at most 32 characters")
        self.integrations = {i.integration_id: i for i in integrations}
        self._connections = {i.integration_id: _Connection(i) for i in integrations}
        self._discovered: dict[str, list[Tool]] = {}
        self._routes: dict[str, tuple[str, str]] = {}
        self._authorizing: dict[str, asyncio.Task] = {}
        self._closed = False

    @classmethod
    def from_config(cls, path: Path = CONFIG_PATH):
        if not path.exists():
            return cls([])
        try:
            config = tomllib.loads(path.read_text())
        except (ValueError, OSError):
            logger.warning("MCP configuration could not be read; integrations are disabled")
            return cls([])
        integrations = []
        entries = config.get("integrations", {})
        if not isinstance(entries, dict):
            logger.warning("Invalid MCP configuration; integrations are disabled")
            return cls([])
        for integration_id, entry in entries.items():
            try:
                if not isinstance(entry, dict):
                    raise ValueError("Integration settings must be a table")
                if not isinstance(entry.get("description", integration_id), str):
                    raise ValueError("description must be text")
                if "url" in entry:
                    if not isinstance(entry["url"], str) or "command" in entry:
                        raise ValueError("Specify one transport")
                    from src.integrations.auth import validate_http_url
                    validate_http_url(entry["url"])
                    server = entry["url"]
                else:
                    server = StdioServerParameters(
                        command=str(Path(entry["command"]).expanduser()), args=entry.get("args", []),
                        cwd=str(Path(entry["cwd"]).expanduser()) if entry.get("cwd") else None,
                    )
                kind = entry.get("auth", "none")
                if kind == "mcp_oauth" and isinstance(server, str):
                    adapter = MCPOAuthAdapter(
                        server, redirect_uri=entry.get("redirect_uri", "http://127.0.0.1:8765/oauth/callback"),
                        client_id=entry.get("client_id"), scopes=entry.get("scopes"),
                        authorization_params=entry.get("authorization_params"),
                    )
                elif kind == "spotify" and isinstance(server, StdioServerParameters) and server.cwd:
                    adapter = SpotifyAuthAdapter(Path(server.cwd), server.command)
                elif kind == "none":
                    adapter = None
                else:
                    raise ValueError("Unsupported authentication/transport combination")
                if "tools" in entry and (not isinstance(entry["tools"], list)
                                        or any(not isinstance(t, str) for t in entry["tools"])):
                    raise ValueError("tools must be a list")
                if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", integration_id):
                    raise ValueError("Invalid integration ID")
                integrations.append(MCPIntegration(
                    integration_id, entry.get("description", integration_id), server, adapter, entry.get("tools"),
                ))
            except (ValueError, KeyError, TypeError):
                logger.warning("Skipping invalid MCP integration configuration")
        return cls(integrations)

    async def __aenter__(self):
        await asyncio.gather(*(self.connect(i) for i in self.integrations))
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        self._closed = True
        tasks = [t for t in self._authorizing.values() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(c.close() for c in self._connections.values()))

    def _failure(self, integration_id, error) -> dict:
        adapter = self.integrations[integration_id].auth
        errors = list(_causes(error))
        safe = next((e for e in errors if isinstance(e, AuthFailure)), None)
        if safe:
            return AuthOutcome("failed", str(safe)).as_dict(integration_id)
        if adapter:
            for cause in errors:
                required = adapter.classify_failure(cause)
                if required:
                    return AuthOutcome("auth_required", required.reason).as_dict(integration_id)
        logger.warning("MCP integration '{}' failed ({})", integration_id, type(errors[0]).__name__)
        return AuthOutcome("failed", "integration_unavailable").as_dict(integration_id)

    async def connect(self, integration_id: str) -> dict:
        if not isinstance(integration_id, str) or integration_id not in self.integrations:
            return AuthOutcome("failed", "unknown_integration").as_dict(integration_id)
        if self._closed:
            return AuthOutcome("failed", "runtime_closed").as_dict(integration_id)
        if (integration_id in self._authorizing
                and self._authorizing[integration_id] is not asyncio.current_task()):
            return AuthOutcome("awaiting_authorization").as_dict(integration_id)
        connection = self._connections[integration_id]
        try:
            async with asyncio.timeout(30):
                client = await connection.start()
                self._discovered[integration_id] = (await client.list_tools()).tools
            return AuthOutcome("connected").as_dict(integration_id)
        except Exception as error:
            cause = connection.error or error
            await connection.close()
            return self._failure(integration_id, cause)

    async def call_tool(self, integration_id: str, name: str, arguments: dict) -> dict:
        if integration_id in self._authorizing:
            return AuthOutcome("awaiting_authorization").as_dict(integration_id)
        connection = self._connections.get(integration_id)
        connected = (AuthOutcome("connected").as_dict(integration_id)
                     if not self._closed and connection and connection.client
                     and integration_id in self._discovered else await self.connect(integration_id))
        if connected["status"] != "connected":
            return connected
        integration = self.integrations[integration_id]
        allowed = {t.name for t in self._discovered[integration_id]
                   if integration.tools is None or t.name in integration.tools}
        if name not in allowed:
            return AuthOutcome("failed", "unknown_tool").as_dict(integration_id)
        connection = self._connections[integration_id]
        try:
            result = await connection.client.session.call_tool(name, arguments, allow_input_required=True)
            if isinstance(result, InputRequiredResult):
                return AuthOutcome("failed", "unsupported_auth_interaction").as_dict(integration_id)
            required = integration.auth.classify_failure(result) if integration.auth else None
            if required:
                return AuthOutcome("auth_required", required.reason).as_dict(integration_id)
            return {
                "status": "failed" if result.is_error else "completed",
                "integration": integration_id,
                "content": [c.model_dump(mode="json") for c in result.content if isinstance(c, TextContent)],
                "structured_content": result.structured_content,
            }
        except Exception as error:
            cause = connection.error or error
            await connection.close()
            return self._failure(integration_id, cause)

    async def authorize(self, integration_id: str, report_progress) -> dict:
        if not isinstance(integration_id, str) or integration_id not in self.integrations:
            return AuthOutcome("failed", "unknown_integration").as_dict(integration_id)
        if self._closed:
            return AuthOutcome("failed", "runtime_closed").as_dict(integration_id)
        if integration_id in self._authorizing:
            return AuthOutcome("awaiting_authorization").as_dict(integration_id)
        adapter = self.integrations[integration_id].auth
        if adapter is None:
            return await self.connect(integration_id)
        self._authorizing[integration_id] = asyncio.current_task()
        try:
            await self._connections[integration_id].close()
            async with asyncio.timeout(AUTH_TIMEOUT):
                result = await adapter.authorize(report_progress)
                if result.status != "connected":
                    return result.as_dict(integration_id)
                return await self.connect(integration_id)
        except TimeoutError:
            return AuthOutcome("timed_out").as_dict(integration_id)
        except Exception as error:
            return self._failure(integration_id, error)
        finally:
            self._authorizing.pop(integration_id, None)

    def tools_schema(self) -> ToolsSchema:
        if not self.integrations:
            return ToolsSchema([])
        ids = list(self.integrations)
        descriptions = "; ".join(f"{i}: {s.description}" for i, s in self.integrations.items())
        properties = {"integration_id": {"type": "string", "enum": ids}}
        schemas = [
            FunctionSchema("connect_integration", f"Connect and discover tools for a service. {descriptions}",
                           properties, ["integration_id"], self._connect_tool),
            FunctionSchema("authorize_integration", "Sign in to a configured service when auth_required is returned. "
                           "Wait for completion before retrying service tools. " + descriptions,
                           properties, ["integration_id"], self._authorize_tool),
        ]
        self._routes = {}
        for integration_id, tools in self._discovered.items():
            allowed = self.integrations[integration_id].tools
            for tool in tools:
                if allowed is not None and tool.name not in allowed:
                    continue
                name = f"{integration_id}__{tool.name}"
                if len(name) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                    name = f"{integration_id}__{hashlib.sha256(tool.name.encode()).hexdigest()[:24]}"
                self._routes[name] = (integration_id, tool.name)
                schemas.append(_MCPSchema(name, tool, self._service_tool))
        return ToolsSchema(schemas)

    async def _connect_tool(self, params: FunctionCallParams):
        result = await self.connect(params.arguments.get("integration_id"))
        params.context.set_tools(self.tools_schema())
        await params.result_callback(result)

    @tool_options(cancel_on_interruption=False, cancellable_by_llm=True, timeout_secs=300)
    async def _authorize_tool(self, params: FunctionCallParams):
        integration_id = params.arguments.get("integration_id")

        async def report(update):
            await params.result_callback(update | {"integration": integration_id}, properties=
                                         FunctionCallResultProperties(is_final=False, run_llm=True))

        result = await self.authorize(integration_id, report)
        params.context.set_tools(self.tools_schema())
        await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=True))

    async def _service_tool(self, params: FunctionCallParams):
        route = self._routes.get(params.function_name)
        result = await self.call_tool(*route, dict(params.arguments)) if route else {
            "status": "failed", "reason": "unknown_tool",
        }
        await params.result_callback(result)
