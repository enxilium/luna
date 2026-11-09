"""Compatibility with captured responses from all 30 pinned Spotify MCP tools."""

import asyncio
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp.types import CallToolResult, TextContent, Tool

from src.integrations.mcp import MCPIntegration, MCPRuntime
from src.integrations.spotify import (
    SpotifyAuthAdapter,
    playback_uri,
    result_error,
    result_identifiers,
)

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "scripts/fixtures/spotify-results.json").read_text()
)


def text_result(text):
    return CallToolResult(content=[TextContent(type="text", text=text)])


class SpotifyResultTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, result):
        runtime = MCPRuntime(
            [MCPIntegration("spotify", "Spotify", "http://127.0.0.1/mcp")]
        )
        runtime._discovered["spotify"] = [
            Tool.model_validate(t) for t in FIXTURE["tools"]
        ]
        runtime._refresh_catalog("spotify")
        call = AsyncMock(return_value=result)
        runtime._connections["spotify"].client = SimpleNamespace(
            session=SimpleNamespace(call_tool=call)
        )
        self.addAsyncCleanup(runtime.close)
        return runtime, call

    async def test_all_captured_protocol_formats_preserve_content_and_status(self):
        self.assertEqual(len(FIXTURE["tools"]), 30)
        self.assertEqual(
            {r["tool"] for r in FIXTURE["rows"]}, {t["name"] for t in FIXTURE["tools"]}
        )
        adapter = SpotifyAuthAdapter(Path("/unused"), "node")
        for row in FIXTURE["rows"]:
            with self.subTest(
                tool=row["tool"], variant=row["variant"], protocol=row["protocol"]
            ):
                response = CallToolResult.model_validate(copy.deepcopy(row["result"]))
                runtime, call = self.runtime(response)
                runtime._spotify_identifiers.add("spotify:track:track1")
                identifier = playback_uri(row["args"])
                if identifier:
                    runtime._spotify_identifiers.add(identifier)
                identifiers = runtime._spotify_identifiers.copy()
                result = await runtime.call_tool("spotify", row["tool"], row["args"])
                expected = (
                    "failed"
                    if row["variant"]
                    in {
                        "failure", "missing-arguments", "no-device", "unknown-volume",
                        "playback-not-started", "playback-paused", "playback-wrong-track",
                        "playback-wrong-device", "playback-muted", "play-request-failure",
                        "playback-wrong-context",
                        "track-missing-album", "track-invalid-album", "track-lookup-failure",
                    }
                    else "completed"
                )
                self.assertEqual(result["status"], expected)
                if row["variant"] == "missing-arguments":
                    call.assert_not_awaited()
                    continue
                self.assertEqual(
                    [c.get("text") for c in result["content"]],
                    [c.text for c in response.content],
                )
                if expected == "failed":
                    self.assertEqual(
                        runtime._spotify_identifiers, identifiers
                    )
                with patch.object(
                    adapter,
                    "_config",
                    return_value={"accessToken": "access", "refreshToken": "refresh"},
                ):
                    self.assertIsNone(adapter.classify_failure(response))

    async def test_real_playlist_text_all_playback_aliases_and_typed_identity(self):
        for args in (
            {"type": "playlist", "id": "playlist1"},
            {"context_uri": "spotify:playlist:playlist1"},
            {"uri": "spotify:playlist:playlist1"},
        ):
            runtime, call = self.runtime(
                text_result(
                    '# Your Spotify Playlists (1-1 of 1)\n\n1. "the hermit" (12 tracks) - ID: playlist1'
                )
            )
            await runtime.call_tool("spotify", "getMyPlaylists", {})
            self.assertEqual(
                runtime._spotify_identifiers, {"spotify:playlist:playlist1"}
            )
            call.return_value = text_result("Now playing: spotify:playlist:playlist1")
            self.assertEqual(
                (await runtime.call_tool("spotify", "playMusic", args))["status"],
                "completed",
            )
            self.assertEqual(call.await_count, 2)
            for invalid in (
                {"uri": "spotify:track:playlist1"},
                {"type": "album", "id": "playlist1"},
                {"uri": "spotify:playlist:playlist1", "type": "track"},
                {"uri": "spotify:playlist:guessed"},
            ):
                self.assertEqual(
                    (await runtime.call_tool("spotify", "playMusic", invalid))[
                        "reason"
                    ],
                    "unresolved_playback_identifier",
                )
            self.assertEqual(call.await_count, 2)

    async def test_text_markdown_json_structured_ids_and_no_device_contamination(self):
        cases = [
            (
                "searchSpotify",
                {"type": "track"},
                text_result(
                    '# Search results\n\n1. "Song" by Artist (3:00) - ID: track1'
                ),
                {"spotify:track:track1"},
            ),
            (
                "getNowPlaying",
                {},
                text_result(
                    "# Currently Paused\n\n**ID**: track1\n\n**Device**: Speaker"
                ),
                {"spotify:track:track1"},
            ),
            (
                "searchSpotify",
                {"type": "album"},
                text_result('{"albums":{"items":[{"id":"album1"}]}}'),
                {"spotify:album:album1"},
            ),
            (
                "searchSpotify",
                {"type": "artist"},
                CallToolResult(
                    content=[],
                    structuredContent={
                        "artists": [{"id": "artist1", "uri": "spotify:artist:artist1"}]
                    },
                ),
                {"spotify:artist:artist1"},
            ),
            (
                "getMyPlaylists",
                {},
                text_result('{"items":[{"id":"playlist1","owner":{"id":"user1"}}]}'),
                {"spotify:playlist:playlist1"},
            ),
            (
                "getAvailableDevices",
                {},
                text_result(
                    "# Available Spotify Devices\n\n1. Speaker (Speaker)\n   Status: ▶ Active | Volume: 50% | ID: device1"
                ),
                set(),
            ),
            (
                "getAvailableDevices",
                {},
                text_result('{"devices":[{"id":"device1"}]}'),
                set(),
            ),
            (
                "searchSpotify",
                {"type": "track"},
                text_result('{"tracks":[{"id":"track1"}],"device":{"id":"device1"}}'),
                {"spotify:track:track1"},
            ),
            (
                "searchSpotify",
                {"type": "episode"},
                text_result('1. "Episode" — Show (3:00) - ID: episode1'),
                set(),
            ),
            (
                "getMyPlaylists",
                {},
                text_result('1. "ID: injected" (12 tracks) - ID: playlist1'),
                {"spotify:playlist:playlist1"},
            ),
            (
                "searchSpotify",
                {"type": "playlist"},
                text_result('1. "Unknown" - ID: undefined'),
                set(),
            ),
            (
                "getMyPlaylists",
                {},
                text_result('Malformed or truncated JSON {"id":"guessed"'),
                set(),
            ),
        ]
        for name, args, result, expected in cases:
            with self.subTest(tool=name, args=args, result=result):
                self.assertEqual(result_identifiers(name, args, result), expected)

    async def test_search_and_play_in_one_batch_remain_ordered(self):
        runtime, call = self.runtime(text_result("Playback stub"))

        async def respond(name, arguments, **kwargs):
            if name == "getMyPlaylists":
                await asyncio.sleep(0.01)
                return text_result('1. "the hermit" (12 tracks) - ID: playlist1')
            return text_result("Now playing: spotify:playlist:playlist1")

        call.side_effect = respond
        search, play = await asyncio.gather(
            runtime.call_tool("spotify", "getMyPlaylists", {}),
            runtime.call_tool(
                "spotify", "playMusic", {"context_uri": "spotify:playlist:playlist1"}
            ),
        )
        self.assertEqual([search["status"], play["status"]], ["completed", "completed"])
        self.assertEqual(
            [c.args[0] for c in call.await_args_list], ["getMyPlaylists", "playMusic"]
        )

    async def test_large_playlist_pages_keep_all_ids_before_context_preview(self):
        text = "# Your Spotify Playlists (1-50 of 50)\n\n" + "\n".join(
            f'{i}. "Playlist {i} ' + "x" * 200 + f'" (12 tracks) - ID: playlist{i}'
            for i in range(1, 51)
        )
        runtime, call = self.runtime(text_result(text))
        result = await runtime.call_tool("spotify", "getMyPlaylists", {})
        self.assertTrue(result["result_truncated"])
        self.assertEqual(len(runtime._spotify_identifiers), 50)
        self.assertIn("spotify:playlist:playlist50", runtime._spotify_identifiers)
        call.return_value = text_result("Now playing: spotify:playlist:playlist50")
        self.assertEqual(
            (
                await runtime.call_tool(
                    "spotify", "playMusic", {"type": "playlist", "id": "playlist50"}
                )
            )["status"],
            "completed",
        )

    async def test_explicit_user_uri_keeps_its_type(self):
        from pipecat.processors.aggregators.llm_context import LLMContext

        runtime, call = self.runtime(
            text_result("Now playing: spotify:playlist:playlist1")
        )
        context = LLMContext(
            messages=[
                {
                    "role": "user",
                    "content": "Play https://open.spotify.com/playlist/playlist1",
                }
            ]
        )
        for args, expected in (
            ({"type": "playlist", "id": "playlist1"}, "completed"),
            ({"type": "track", "id": "playlist1"}, "failed"),
        ):
            callback = AsyncMock()
            await runtime._service_tool(
                SimpleNamespace(
                    function_name="spotify__playMusic",
                    context=context,
                    arguments=args,
                    result_callback=callback,
                )
            )
            self.assertEqual(callback.call_args.args[0]["status"], expected)
        self.assertEqual(call.await_count, 1)

    def test_text_errors_and_success_receipts(self):
        for text in (
            "Error: invalid request",
            "Error playing music: No Spotify devices found",
            "Error searching for tracks: 401",
        ):
            result = text_result(text)
            self.assertTrue(result_error(result))
            self.assertTrue(result.is_error)
            self.assertEqual(
                result_identifiers("searchSpotify", {"type": "track"}, result), set()
            )
        for text in (
            "Playback paused",
            "Nothing is currently playing on Spotify",
            'Playlist: "Error 401"',
            "No available devices found. Make sure Spotify is open on at least one device.",
        ):
            result = text_result(text)
            self.assertFalse(result_error(result))
            self.assertFalse(result.is_error)
        self.assertIsNone(
            playback_uri({"uri": "spotify:track:known", "type": "playlist"})
        )


if __name__ == "__main__":
    unittest.main()
