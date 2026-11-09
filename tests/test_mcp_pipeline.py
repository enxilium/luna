"""Real Pipecat Gemini orchestration against a simulated Live session and local OAuth."""

import asyncio
import os
import socket
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import aiohttp
from google.genai import types
from pipecat.frames.frames import InputTextRawFrame, InterruptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.workers.runner import WorkerRunner

from src.agent.services import build_llm_service
from src.integrations.auth import MCPOAuthAdapter
from src.integrations.mcp import MCPIntegration, MCPRuntime
from tests.test_integrations import MemoryStorage, OAuthServer


class FakeGemini:
    def __init__(self, on_text=None, on_result=None, on_commit=None):
        self.sessions = []
        self.on_text, self.on_result, self.on_commit = on_text, on_result, on_commit
        self.call_count = 0

    @asynccontextmanager
    async def connect(self, *, model, config):
        await asyncio.sleep(0.01)
        session = FakeSession(
            self,
            types.LiveConnectConfig.model_validate(
                config.model_dump(exclude_none=True)
            ),
        )
        self.sessions.append(session)
        try:
            yield session
        finally:
            await session.close()


class FakeSession:
    def __init__(self, server, config):
        self.server, self.config = server, config
        self.messages = asyncio.Queue()
        self.audio = []
        self.seeds = []
        self.responses = []
        self.closed = False
        self.functions = {
            f.name: f for t in config.tools or [] for f in t.function_declarations or []
        }

    async def receive(self):
        while (message := await self.messages.get()) is not None:
            yield message

    async def close(self):
        if not self.closed:
            self.closed = True
            await self.messages.put(None)

    async def send_client_content(self, *, turns=None, turn_complete=False):
        self.seeds.append(turns)
        text = (
            "".join(part.text or "" for part in turns.parts)
            if isinstance(turns, types.Content)
            else ""
        )
        if (
            turn_complete
            and text
            and not text.startswith("[Function")
            and self.server.on_text
        ):
            await self.server.on_text(self, text)
        elif turn_complete and self.server.on_commit:
            await self.server.on_commit(self)

    async def send_realtime_input(self, *, text=None, audio=None, **kwargs):
        if audio:
            self.audio.append(audio)
        if text and text.strip() and self.server.on_text:
            await self.server.on_text(self, text)

    async def send_tool_response(self, *, function_responses):
        responses = (
            function_responses
            if isinstance(function_responses, list)
            else [function_responses]
        )
        self.responses.extend(responses)
        if self.server.on_result:
            for response in responses:
                await self.server.on_result(self, response)

    async def call(self, name, args=None):
        self.server.call_count += 1
        await self.messages.put(
            types.LiveServerMessage(
                tool_call=types.LiveServerToolCall(
                    function_calls=[
                        types.FunctionCall(
                            id=f"call-{self.server.call_count}",
                            name=name,
                            args=args or {},
                        )
                    ]
                )
            )
        )

    async def say(self, text):
        await self.messages.put(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    model_turn=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                inline_data=types.Blob(
                                    data=b"\x01\0" * 480,
                                    mime_type="audio/pcm;rate=24000",
                                )
                            )
                        ],
                    )
                )
            )
        )
        await self.messages.put(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    output_transcription=types.Transcription(text=text)
                )
            )
        )
        await self.messages.put(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(turn_complete=True)
            )
        )


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def run_flow(self, *, cancel=False):
        server = await OAuthServer().__aenter__()
        self.addAsyncCleanup(server.__aexit__)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        auth = MCPOAuthAdapter(
            server.url,
            f"http://127.0.0.1:{port}/callback",
            storage=MemoryStorage(server.url),
        )
        runtime = MCPRuntime(
            [MCPIntegration("test", "Private test value", server.url, auth)]
        )
        self.addAsyncCleanup(runtime.close)
        await runtime.connect("test")
        waiting, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        browsers = []
        loop = asyncio.get_running_loop()

        async def browse(url):
            waiting.set()
            await release.wait()
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    await response.read()

        def open_browser(url):
            browsers.append(asyncio.run_coroutine_threadsafe(browse(url), loop))
            return True

        async def text(session, value):
            if value.startswith("Cancel"):
                self.assertIn("cancel_authorize_integration", session.functions)
                await session.call("cancel_authorize_integration")
            else:
                await session.call("connect_integration", {"integration_id": "test"})

        async def result(session, response):
            if response.name == "connect_integration":
                await session.call("authorize_integration", {"integration_id": "test"})
            elif response.name == "test__read_value":
                await session.say("The value is forty-two.")
            elif response.name == "cancel_authorize_integration":
                await session.say("Sign-in was cancelled.")

        async def commit(session):
            if "test__read_value" in session.functions:
                self.assertNotIn("tools/call", server.calls)
                await session.call("test__read_value")

        fake = FakeGemini(text, result, commit)
        with (
            patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}),
            patch("src.integrations.auth.webbrowser.open", open_browser),
        ):
            llm = build_llm_service(runtime)
            llm._client.aio.live.connect = fake.connect
            self.addAsyncCleanup(llm._client.aio.aclose)
            self.addCleanup(llm._client.close)
            user, assistant = LLMContextAggregatorPair(llm.context)

            @assistant.event_handler("on_assistant_turn_stopped")
            async def on_reply(aggregator, message):
                finished.set()

            worker = PipelineWorker(
                Pipeline([user, llm, assistant]),
                enable_rtvi=False,
                idle_timeout_secs=None,
                app_resources=runtime,
            )
            runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
            await runner.add_workers(worker)
            running = asyncio.create_task(runner.run())
            try:
                async with asyncio.timeout(10):
                    await llm.wait_until_ready()
                    self.assertIn("connect_integration", llm._functions)
                    self.assertEqual(
                        fake.sessions[0].functions["authorize_integration"].behavior,
                        types.Behavior.NON_BLOCKING,
                    )
                    await worker.queue_frame(
                        InputTextRawFrame("Read the private test value.")
                    )
                    await waiting.wait()
                    await worker.queue_frame(InterruptionFrame())
                    await asyncio.sleep(0.01)
                    self.assertIn("test", runtime._authorizing)
                    if cancel:
                        await worker.queue_frame(InputTextRawFrame("Cancel login."))
                    else:
                        release.set()
                    await finished.wait()
                    if cancel:
                        self.assertNotIn("tools/call", server.calls)
                        self.assertFalse(runtime._authorizing)
                    else:
                        self.assertEqual(server.calls.count("tools/call"), 1)
                        self.assertEqual(len(fake.sessions), 2)
                        self.assertIn("test__read_value", fake.sessions[-1].functions)
                        self.assertIn("connected", str(fake.sessions[-1].seeds))
                    self.assertTrue(
                        all(
                            s.functions["authorize_integration"].behavior
                            == types.Behavior.NON_BLOCKING
                            for s in fake.sessions
                        )
                    )
            finally:
                for browser in browsers:
                    browser.cancel()
                await worker.cancel()
                await asyncio.wait_for(running, 3)

    async def test_authorization_refreshes_full_catalog_without_action_replay(self):
        await self.run_flow()

    async def test_interruption_keeps_login_and_explicit_cancel_stops_it(self):
        await self.run_flow(cancel=True)


class LivePipelineTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(
        os.environ.get("LUNA_LIVE_TESTS") == "1",
        "Requires GOOGLE_API_KEY and Gemini Live access",
    )
    async def test_real_gemini_login_and_catalog_refresh_with_synthetic_private_data(
        self,
    ):
        server = await OAuthServer().__aenter__()
        self.addAsyncCleanup(server.__aexit__)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        auth = MCPOAuthAdapter(
            server.url,
            f"http://127.0.0.1:{port}/callback",
            storage=MemoryStorage(server.url),
        )
        runtime = MCPRuntime(
            [MCPIntegration("test", "Private test value", server.url, auth)]
        )
        self.addAsyncCleanup(runtime.close)
        await runtime.connect("test")
        loop = asyncio.get_running_loop()
        browsers = []

        async def browse(url):
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    await response.read()

        def open_browser(url):
            browsers.append(asyncio.run_coroutine_threadsafe(browse(url), loop))
            return True

        llm = build_llm_service(runtime)
        self.addAsyncCleanup(llm._client.aio.aclose)
        self.addCleanup(llm._client.close)
        user, assistant = LLMContextAggregatorPair(llm.context)
        finished = asyncio.Event()
        replies = []

        @assistant.event_handler("on_assistant_turn_stopped")
        async def on_reply(aggregator, message):
            replies.append(message.content)
            text = message.content.lower()
            if "tools/call" in server.calls and ("42" in text or "forty" in text):
                finished.set()

        worker = PipelineWorker(
            Pipeline([user, llm, assistant]),
            enable_rtvi=False,
            idle_timeout_secs=None,
            app_resources=runtime,
        )
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)
        with patch("src.integrations.auth.webbrowser.open", open_browser):
            running = asyncio.create_task(runner.run())
            try:
                await llm.wait_until_ready()
                await worker.queue_frame(
                    InputTextRawFrame(
                        "Read the private value from the test integration. Connect first; if sign-in is required, authorize it, wait for completion, then read and report the actual value."
                    )
                )
                await asyncio.wait_for(finished.wait(), 60)
                self.assertEqual(server.calls.count("tools/call"), 1)
                self.assertEqual(len(browsers), 1)
            finally:
                for browser in browsers:
                    browser.cancel()
                await worker.cancel()
                await asyncio.wait_for(running, 5)
