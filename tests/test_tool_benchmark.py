"""Benchmark correctness requires the requested action, identity and verification order."""

import unittest

from scripts.benchmark_tools import (
    PLAYLIST_URI,
    TRACK_URI,
    answer_correct,
    arguments_correct,
)


def call(name, arguments, integration="spotify"):
    return {"integration": integration, "name": name, "arguments": arguments}


class BenchmarkCorrectnessTests(unittest.TestCase):
    def test_playlist_requires_listing_before_one_correct_playlist(self):
        listing = call("getMyPlaylists", {})
        play = call("playMusic", {"context_uri": PLAYLIST_URI})
        self.assertTrue(arguments_correct("playlist", [listing, play], False))
        for calls in (
            [play],
            [play, listing],
            [listing, play, play],
            [listing, call("playMusic", {"uri": TRACK_URI})],
        ):
            self.assertFalse(arguments_correct("playlist", calls, False))

    def test_unsupported_refusal_accepts_normal_spoken_contractions(self):
        for text in (
            "I can't delete calendars.",
            "I don't have the ability to delete an entire calendar.",
            "I don’t have that ability.",
        ):
            self.assertTrue(answer_correct("unsupported", text))
        self.assertFalse(answer_correct("unsupported", "I deleted your calendar."))

    def test_playback_requires_search_before_one_correct_recording(self):
        search = call(
            "searchSpotify", {"query": "Bohemian Rhapsody Queen", "type": "track"}
        )
        play = call("playMusic", {"uri": TRACK_URI})
        self.assertTrue(arguments_correct("playback", [search, play], False))
        self.assertTrue(arguments_correct("history_playback", [search, play], False))
        self.assertTrue(arguments_correct("explicit_playback", [play], False))
        for calls in [
            [play],
            [play, search],
            [search, call("playMusic", {"uri": "spotify:track:cover-recording"})],
            [search, play, play],
            [
                call("searchSpotify", {"query": "Bohemian Rhapsody", "type": "track"}),
                play,
            ],
        ]:
            self.assertFalse(arguments_correct("playback", calls, False))

    def test_doc_edit_requires_one_edit_and_subsequent_verification(self):
        edit = call("update_doc", {"documentId": "doc-example"}, "google_docs")
        read = call("read_doc", {"documentId": "doc-example"}, "google_docs")
        self.assertTrue(arguments_correct("docs", [edit, read], True))
        drive_read = call(
            "read_file_content", {"fileId": "doc-example"}, "google_drive"
        )
        self.assertTrue(arguments_correct("docs", [edit, drive_read], True))
        for calls, replaced in [
            ([read, edit], True),
            ([edit, edit, read], True),
            ([edit, read], False),
        ]:
            self.assertFalse(arguments_correct("docs", calls, replaced))


class LiveBenchmarkTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_only_boundary_does_not_cut_off_later_audio(self):
        import asyncio
        import os
        from unittest.mock import patch

        from google.genai import types

        from scripts import benchmark_tools
        from src.agent.services import build_llm_service
        from src.integrations.mcp import MCPRuntime
        from tests.test_mcp_pipeline import FakeGemini

        async def text(session, prompt):
            await session.messages.put(
                types.LiveServerMessage(
                    server_content=types.LiveServerContent(
                        output_transcription=types.Transcription(text="One moment."),
                        turn_complete=True,
                    )
                )
            )
            await asyncio.sleep(0.05)
            await session.say("Here is the answer.")

        fake = FakeGemini(on_text=text)
        runtime = MCPRuntime([])
        self.addAsyncCleanup(runtime.close)
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}):
            llm = build_llm_service(runtime)
        llm._client.aio.live.connect = fake.connect
        with patch.object(benchmark_tools, "build_llm_service", return_value=llm):
            result = await benchmark_tools.run_case(runtime, "Hello.", timeout=2)
        self.assertIsNotNone(result["first_audio_seconds"])
        self.assertEqual(result["errors"], [])
        self.assertIn("Here is the answer.", result["spoken_text"])


if __name__ == "__main__":
    unittest.main()
