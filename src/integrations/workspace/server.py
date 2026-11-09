"""Native MCP server shared by Luna and the standalone stdio entry point."""

import json
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfoNotFoundError

import httpx
from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from src.integrations.auth import AuthFailure, AuthRequired
from src.integrations.schemas import invalid_arguments

from . import TOOLS
from .auth import GoogleAPIAuthAdapter
from .calendar import Calendar
from .chat import Chat
from .client import APIError, GoogleAPI
from .documents import Docs, Sheets, Slides
from .drive import Drive
from .gmail import Gmail
from .people import People

TOOLSETS = {
    "gmail": Gmail,
    "drive": Drive,
    "docs": Docs,
    "sheets": Sheets,
    "slides": Slides,
    "calendar": Calendar,
    "chat": Chat,
    "people": People,
}


def build_server(service, api=None, *, auth=None):
    tools = {t["name"]: Tool.model_validate(t) for t in TOOLS[service]}
    for name in tools:
        if not callable(getattr(TOOLSETS[service], name, None)):
            raise ValueError("Missing Workspace implementation: " + name)

    @asynccontextmanager
    async def lifespan(server):
        if api is not None:
            yield api
        else:
            async with httpx.AsyncClient(follow_redirects=False) as http:
                yield GoogleAPI(auth or GoogleAPIAuthAdapter(service), http)

    async def list_tools(context, params):
        return ListToolsResult(tools=list(tools.values()))

    async def call_tool(context, params):
        base = api or context.lifespan_context
        request_api = GoogleAPI(base.auth, base.http)
        toolset = TOOLSETS[service](request_api)
        name, arguments = params.name, params.arguments or {}
        error = (
            invalid_arguments(tools[name], arguments)
            if name in tools
            else {"reason": "unknown_tool"}
        )
        failed = True
        try:
            if error:
                result = {"status": "failed", **error}
            else:
                result = await getattr(toolset, name)(**arguments)
                failed = False
        except APIError as error:
            result = error.result
        except AuthRequired as error:
            result = {"status": "auth_required", "reason": error.reason}
        except AuthFailure as error:
            result = {"status": "failed", "reason": str(error)}
        except (ValueError, ZoneInfoNotFoundError) as error:
            result = {
                "status": "failed",
                "reason": "invalid_arguments",
                "message": str(error),
            }
        except (httpx.HTTPError, TimeoutError):
            result = {
                "status": "failed",
                "reason": "api_connection_failed",
                "notice": "The request outcome may be unknown. Do not automatically repeat mutations.",
            }
        except Exception:
            result = {"status": "failed", "reason": "backend_error"}
        if failed and request_api.completed_writes:
            result["completed_requests"] = request_api.completed_writes.copy()
        return CallToolResult(
            isError=failed,
            structuredContent=result,
            content=[
                TextContent(type="text", text=json.dumps(result, ensure_ascii=False))
            ],
        )

    return Server(
        "luna-workspace-" + service,
        version="1",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        lifespan=lifespan,
    )
