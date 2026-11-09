"""Full publication keeps canonical validation, ordering, paging and allowlists."""

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from google.genai.types import LiveConnectConfig
from mcp.types import CallToolResult, TextContent, ImageContent, Tool
from pipecat.processors.aggregators.llm_context import LLMContext

from src.agent.services import _MCPGeminiAdapter
from src.integrations.mcp import MCPIntegration, MCPRuntime
from src.integrations.schemas import invalid_arguments


def tool(name, description="", schema=None):
    return Tool(
        name=name,
        description=description or name,
        inputSchema=schema or {"type": "object"},
    )


class MCPToolsTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self):
        runtime = MCPRuntime(
            [
                MCPIntegration(i, i, "http://127.0.0.1/mcp")
                for i in ("google_calendar", "spotify", "offline")
            ]
        )
        for i, tools in {
            "google_calendar": [
                tool("list_events"),
                tool("get_calendar"),
                tool("create_event"),
            ],
            "spotify": [
                tool("searchSpotify"),
                tool("playMusic"),
                tool("getNowPlaying"),
            ],
        }.items():
            runtime._discovered[i] = tools
            runtime._refresh_catalog(i)
        return runtime

    async def test_paginated_catalog_is_atomic_and_repeated_cursors_fail(self):
        runtime = self.runtime()
        pages = [
            SimpleNamespace(tools=[tool("read_one")], next_cursor="page2"),
            SimpleNamespace(tools=[tool("read_two")], next_cursor=None),
        ]
        client = SimpleNamespace(list_tools=AsyncMock(side_effect=pages))
        runtime._connections["offline"].client = client
        await runtime._discover("offline")
        self.assertEqual(
            [t.name for t in runtime._discovered["offline"]], ["read_one", "read_two"]
        )
        self.assertEqual(
            client.list_tools.call_args_list[-1].kwargs, {"cursor": "page2"}
        )
        client.list_tools = AsyncMock(return_value=pages[0])
        with self.assertRaises(ValueError):
            await runtime._discover("offline")
        self.assertEqual(len(runtime._discovered["offline"]), 2)
        connection = runtime._connections["offline"]
        watch = asyncio.create_task(connection._watch_catalog())
        runtime._discover = (
            AsyncMock()
        )  # Bound callback uses original method; observe refreshed tools instead.
        client.list_tools = AsyncMock(
            return_value=SimpleNamespace(tools=[tool("read_new")], next_cursor=None)
        )
        await connection._notification(
            SimpleNamespace(method="notifications/tools/list_changed")
        )
        async with asyncio.timeout(0.5):
            while "offline__read_new" not in runtime._catalog:
                await asyncio.sleep(0.001)
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)

    async def test_dedup_large_results_pages_expiry_and_validation(self):
        runtime = self.runtime()
        value = {"value": "x" * 16000, "receipt": "receipt-123"}
        media = ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png")
        call = AsyncMock(
            return_value=CallToolResult(
                content=[TextContent(type="text", text=json.dumps(value)), media],
                structuredContent=value,
            )
        )
        runtime._connections["google_calendar"].client = SimpleNamespace(
            session=SimpleNamespace(call_tool=call)
        )
        result = await runtime.call_tool("google_calendar", "list_events", {})
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["result_truncated"])
        stored = json.loads(runtime._results[result["result_id"]])
        self.assertEqual(stored["content"], [media.model_dump(mode="json")])
        self.assertEqual(stored["structured_content"], value)
        callback = AsyncMock()
        chunks, offset = [], 0
        while offset is not None:
            await runtime._read_result_tool(
                SimpleNamespace(
                    arguments={"result_id": result["result_id"], "offset": offset},
                    result_callback=callback,
                )
            )
            page = callback.call_args.args[0]
            self.assertLessEqual(len(page["text"]), 4096)
            chunks.append(page["text"])
            offset = page["next_offset"]
        self.assertEqual(json.loads("".join(chunks)), stored)
        await runtime._read_result_tool(
            SimpleNamespace(
                arguments={"result_id": result["result_id"], "limit": 10000},
                result_callback=callback,
            )
        )
        self.assertEqual(callback.call_args.args[0]["reason"], "invalid_arguments")
        runtime._connections["google_calendar"].client = None
        await runtime.close()
        await runtime._read_result_tool(
            SimpleNamespace(
                arguments={"result_id": result["result_id"]}, result_callback=callback
            )
        )
        self.assertEqual(callback.call_args.args[0]["reason"], "expired_result")

    def test_all_recorded_tools_and_complete_schemas_are_published(self):
        snapshot = json.loads(
            (
                Path(__file__).parents[1] / "scripts/fixtures/mcp-catalog.json"
            ).read_text()
        )
        runtime = MCPRuntime(
            [
                MCPIntegration(
                    i,
                    r["description"],
                    "http://127.0.0.1/mcp",
                    tools=r.get("allowlist"),
                )
                for i, r in snapshot.items()
            ]
        )
        for i, r in snapshot.items():
            runtime._discovered[i] = [Tool.model_validate(t) for t in r["tools"]]
            runtime._refresh_catalog(i)
        schema = runtime.tools_schema()
        self.assertEqual(len(runtime._catalog), 65)
        self.assertEqual(len(schema.standard_tools), 68)
        self.assertNotIn("search_tools", [t.name for t in schema.standard_tools])
        self.assertNotIn("get_tool_help", [t.name for t in schema.standard_tools])
        docs = next(
            t for t in schema.standard_tools if t.name == "google_docs__update_doc"
        )
        self.assertGreater(len(docs.description), 30000)
        config = LiveConnectConfig(
            tools=_MCPGeminiAdapter().to_provider_tools_format(schema)
        )
        self.assertEqual(len(config.tools[0].function_declarations), 68)
        published = next(
            t for t in config.tools[0].function_declarations if t.name == docs.name
        )
        self.assertEqual(
            published.parameters_json_schema, docs.to_default_dict()["parameters"]
        )
        self.assertIsNone(published.parameters)
        revision = runtime.catalog_revision
        runtime._refresh_catalog("google_docs")
        self.assertEqual(runtime.catalog_revision, revision)
        runtime._discovered["google_docs"] = []
        runtime._refresh_catalog("google_docs")
        self.assertNotIn("google_docs__update_doc", runtime._routes)
        self.assertGreater(runtime.catalog_revision, revision)

    async def test_validation_and_allowlist_still_block_unpublished_direct_calls(self):
        runtime = self.runtime()
        spec = tool(
            "read_value",
            schema={
                "type": "object",
                "properties": {"value": {"type": "integer", "minimum": 3}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        runtime._discovered["google_calendar"] = [spec]
        runtime._refresh_catalog("google_calendar")
        call = AsyncMock(
            return_value=CallToolResult(content=[TextContent(type="text", text="ok")])
        )
        runtime._connections["google_calendar"].client = SimpleNamespace(
            session=SimpleNamespace(call_tool=call)
        )
        for arguments in [{"value": 2}, {"value": True}, {"value": 3, "extra": 1}, {}]:
            self.assertEqual(
                (await runtime.call_tool("google_calendar", "read_value", arguments))[
                    "reason"
                ],
                "invalid_arguments",
            )
        call.assert_not_awaited()
        self.assertEqual(
            (await runtime.call_tool("google_calendar", "read_value", {"value": 3}))[
                "status"
            ],
            "completed",
        )
        runtime.integrations["google_calendar"].tools = []
        runtime._refresh_catalog("google_calendar")
        self.assertEqual(
            (await runtime._invoke_tool("google_calendar__read_value", {"value": 3}))[
                "reason"
            ],
            "unknown_tool",
        )
        self.assertEqual(
            (await runtime.call_tool("google_calendar", "read_value", {"value": 3}))[
                "reason"
            ],
            "unknown_tool",
        )
        self.assertEqual(call.await_count, 1)

    def test_batch_operation_wrappers_remain_validated(self):
        spec = tool(
            "update_doc",
            description="The list of possible updates is:\n- `replaceAllText`: Replace text.",
            schema={"type": "object", "properties": {"requests": {"type": "array"}}},
        )
        for args in [
            {"requests": []},
            {"requests": [{"criteria": {}}]},
            {"requests": [{"replaceAllText": {"criteria": {}, "replaceText": "new"}}]},
        ]:
            self.assertIsNotNone(invalid_arguments(spec, args))
        self.assertIsNone(
            invalid_arguments(
                spec,
                {
                    "requests": [
                        {
                            "replaceAllText": {
                                "containsText": {"text": "old", "matchCase": True},
                                "replaceText": "new",
                            }
                        }
                    ]
                },
            )
        )

    async def test_spotify_identifiers_are_resolved_or_explicitly_supplied(self):
        runtime = self.runtime()
        call = AsyncMock(
            return_value=CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text='{"tracks":[{"uri":"spotify:track:resolved"}]}',
                    )
                ]
            )
        )
        runtime._connections["spotify"].client = SimpleNamespace(
            session=SimpleNamespace(call_tool=call)
        )
        self.assertEqual(
            (
                await runtime.call_tool(
                    "spotify", "playMusic", {"uri": "spotify:track:guessed"}
                )
            )["reason"],
            "unresolved_playback_identifier",
        )
        call.assert_not_awaited()
        await runtime.call_tool("spotify", "searchSpotify", {})
        self.assertEqual(
            (
                await runtime.call_tool(
                    "spotify", "playMusic", {"uri": "spotify:track:resolved"}
                )
            )["status"],
            "completed",
        )
        callback = AsyncMock()
        await runtime._service_tool(
            SimpleNamespace(
                function_name="spotify__playMusic",
                context=LLMContext(
                    messages=[
                        {
                            "role": "user",
                            "content": "Play https://open.spotify.com/intl-en/track/supplied",
                        }
                    ]
                ),
                arguments={"uri": "spotify:track:supplied"},
                result_callback=callback,
            )
        )
        self.assertEqual(callback.call_args.args[0]["status"], "completed")
        runtime._connections["spotify"].client = None
        await runtime.close()
        self.assertFalse(runtime._spotify_identifiers)

    async def test_calls_to_one_integration_execute_in_order(self):
        runtime = self.runtime()
        runtime._discovered["google_calendar"] = [
            tool("write_value"),
            tool("read_value"),
        ]
        runtime._refresh_catalog("google_calendar")
        modified = False

        async def call(name, arguments, **kwargs):
            nonlocal modified
            if name == "write_value":
                await asyncio.sleep(0.01)
                modified = True
            else:
                self.assertTrue(modified)
            return CallToolResult(content=[TextContent(type="text", text="ok")])

        runtime._connections["google_calendar"].client = SimpleNamespace(
            session=SimpleNamespace(call_tool=call)
        )
        await asyncio.gather(
            runtime.call_tool("google_calendar", "write_value", {}),
            runtime.call_tool("google_calendar", "read_value", {}),
        )
