"""Gemini model settings, full JSON schemas, session refresh and failure readiness."""

import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


from src.agent import services


class GeminiServiceTests(unittest.IsolatedAsyncioTestCase):
    def build(self, runtime=None, **kwargs):
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}):
            llm = services.build_llm_service(runtime, **kwargs)
        self.addAsyncCleanup(llm._client.aio.aclose)
        self.addCleanup(llm._client.close)
        return llm

    def test_missing_key_fails_before_connecting(self):
        with (
            patch.dict(os.environ, {"GOOGLE_API_KEY": ""}),
            patch.object(services, "ENV_PATH", Path("/nonexistent/.env")),
        ):
            with self.assertRaisesRegex(ValueError, "GOOGLE_API_KEY"):
                services.build_llm_service()

    async def test_model_uses_native_audio_and_provider_compression(self):
        llm = self.build()
        self.assertEqual(llm.settings.model, "gemini-3.8-live")
        self.assertEqual(llm.settings.modalities.value, "AUDIO")
        self.assertIsNone(llm._resolved_thinking_config())
        self.assertFalse(llm.settings.enable_affective_dialog)
        self.assertEqual(llm.settings.proactivity, {})
        self.assertEqual(llm.settings.context_window_compression, {"enabled": True})
        self.assertFalse(llm._inference_on_context_initialization)
        self.assertIs(llm.context, llm._context)
        self.assertNotIn("end_conversation", llm._functions)
        self.assertNotIn("end_conversation", llm.settings.system_instruction)

    async def test_failed_connection_cannot_claim_readiness(self):
        llm = self.build()
        llm.push_error = AsyncMock()

        async def expire(waiter, timeout):
            waiter.close()
            raise TimeoutError

        with patch("src.agent.services.asyncio.wait_for", expire):
            with self.assertRaisesRegex(ConnectionError, "could not connect"):
                await llm.wait_until_ready()
        self.assertFalse(llm.ready.is_set())
        llm.push_error.assert_not_awaited()

    async def test_deferred_service_blocks_connections_and_catalog_refresh_until_wake(self):
        llm = self.build(defer_connection=True)
        with patch.object(services.GeminiLiveLLMService, "_connect", new_callable=AsyncMock) as connect:
            await llm._connect()
            connect.assert_not_awaited()
            llm.integrations.catalog_revision += 1
            with (
                patch.object(llm, "_refresh_tools", new_callable=AsyncMock) as refresh,
                patch.object(llm, "wait_until_ready", new_callable=AsyncMock),
            ):
                await llm._handle_context(llm.context)
                refresh.assert_not_awaited()
                await llm.activate()
                refresh.assert_awaited_once_with(llm.context, respond=False)
            await llm._connect()
            connect.assert_awaited_once()

    async def test_late_tool_result_after_catalog_refresh_is_data_not_reexecution(self):
        llm = self.build()
        session = AsyncMock()
        llm._session = session
        llm._ready_for_realtime_input = True
        llm._retired_calls["old"] = "calendar__read_value"
        await llm._tool_result(
            "old", "tool_call_result", {"status": "completed", "value": 42}
        )
        session.send_client_content.assert_awaited_once()
        content = session.send_client_content.call_args.kwargs["turns"]
        self.assertIn("calendar__read_value", content.parts[0].text)
        self.assertIn("42", content.parts[0].text)
        self.assertNotIn("old", llm._retired_calls)
        llm._session = None

    async def test_end_retires_inflight_calls_and_blocks_idle_tool_results(self):
        llm = self.build(defer_connection=True)
        llm._activated = True
        llm._tool_call_id_to_name["late"] = "calendar__read_value"
        llm._session_resumption_handle = "old-session"
        session = AsyncMock()
        llm._session = session
        with (
            patch.object(llm, "broadcast_interruption", new_callable=AsyncMock) as interrupt,
            patch.object(llm, "_disconnect", new_callable=AsyncMock) as disconnect,
        ):
            await llm._end_conversation()
            await llm._end_conversation()
        interrupt.assert_awaited_once()
        disconnect.assert_awaited_once()
        self.assertIsNone(llm._session_resumption_handle)
        self.assertFalse(llm._activated)
        await llm._tool_result("late", "tool_call_result", {"value": 42})
        session.send_client_content.assert_not_awaited()
        session.send_tool_response.assert_not_awaited()
        llm._activated = True
        llm._ready_for_realtime_input = True
        await llm._tool_result("late", "tool_call_result", {"value": 42})
        session.send_client_content.assert_awaited_once()
        session.send_tool_response.assert_not_awaited()
        self.assertIn("calendar__read_value", str(session.send_client_content.call_args))
        llm._session = None

    async def test_typed_request_commits_an_explicit_user_turn(self):
        llm = self.build()
        session = AsyncMock()
        llm._session = session
        llm._ready_for_realtime_input = True
        await llm._send_user_text("Check my calendar.")
        session.send_client_content.assert_awaited_once()
        args = session.send_client_content.call_args.kwargs
        self.assertTrue(args["turn_complete"])
        self.assertEqual(args["turns"].role, "user")
        self.assertEqual(args["turns"].parts[0].text, "Check my calendar.")
        session.send_realtime_input.assert_awaited_once_with(text=" ")
        llm._session = None
