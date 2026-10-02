"""Native Pipecat async tools; opt into local Ollama/VoiceStudio with LUNA_LIVE_TESTS=1."""

import asyncio
import json
import os
import socket
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web
from loguru import logger
from pipecat.frames.frames import (
    FunctionCallResultFrame, InterruptionFrame, LLMMessagesAppendFrame, TTSAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair, LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from src.agent.services import build_llm_service, build_tts_service
from src.integrations.auth import MCPOAuthAdapter
from src.integrations.mcp import MCPIntegration, MCPRuntime
from tests.test_integrations import MemoryStorage, OAuthServer


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        logger.disable("pipecat")

    @classmethod
    def tearDownClass(cls):
        logger.enable("pipecat")

    async def run_flow(self, *, cancel=False, live=False):
        server = await OAuthServer().__aenter__()
        self.addAsyncCleanup(server.__aexit__)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        storage = MemoryStorage(server.url)
        auth = MCPOAuthAdapter(server.url, f"http://127.0.0.1:{port}/callback", storage=storage)
        runtime = MCPRuntime([MCPIntegration("test", "Private test value", server.url, auth)])
        self.addAsyncCleanup(runtime.close)
        await runtime.connect("test")
        release = asyncio.Event()
        browsers = []
        loop = asyncio.get_running_loop()

        async def browse(url):
            await release.wait()
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    await response.read()

        def open_browser(url):
            browsers.append(asyncio.run_coroutine_threadsafe(browse(url), loop))
            return True

        browser = patch("src.integrations.auth.webbrowser.open", open_browser)
        browser.start()
        self.addCleanup(browser.stop)
        self.addCleanup(lambda: [f.cancel() for f in browsers])
        requests = []

        async def chat(request):
            body = await request.json()
            requests.append(body)
            messages = " ".join(str(m.get("content", "")) for m in body["messages"] if m["role"] != "system")
            tool, args = None, {}
            if 'Cancel login.' in messages and not cancelled.is_set():
                tool, text = "cancel_authorize_integration", "I will cancel sign-in."
            elif cancelled.is_set():
                text = "Sign-in was cancelled."
            elif "tools/call" in server.calls:
                text = "The value is forty-two."
            elif any(t["function"]["name"] == "test__read_value" for t in body["tools"]):
                tool, text = "test__read_value", "I will read it now."
            elif runtime._authorizing:
                text = "Please finish signing in."
            else:
                tool = "authorize_integration" if '"auth_required"' in messages else "connect_integration"
                args = {"integration_id": "test"}
                text = "You need to sign in." if tool == "authorize_integration" else "Let me check."
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            deltas = [{"role": "assistant", "content": text}]
            if tool:
                deltas.append({"tool_calls": [{"index": 0, "id": f"call-{len(requests)}", "type": "function",
                                                "function": {"name": tool, "arguments": json.dumps(args)}}]})
            for delta in deltas:
                chunk = {"id": "test", "object": "chat.completion.chunk", "created": 0, "model": "test",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
            return response

        if live:
            llm = build_llm_service()
            llm.append_system_instruction(
                "Read the user's private value using test tools. Call connect_integration first. "
                "If auth_required, call authorize_integration. Await its connected result, then "
                "call test__read_value and say the returned value. While it runs, just ask the user "
                "to finish signing in. Do not cancel it unless the user explicitly requests cancellation."
            )
        else:
            app = web.Application()
            app.router.add_post("/v1/chat/completions", chat)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            self.addAsyncCleanup(runner.cleanup)
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"
            llm = OLLamaLLMService(base_url=url, settings=OLLamaLLMService.Settings(model="test"))

        waiting, connected, completed, interrupted, cancelled = [asyncio.Event() for _ in range(5)]
        spoken, results, audio = [], [], []

        class Probe(FrameProcessor):
            async def process_frame(probe, frame, direction):
                await super().process_frame(frame, direction)
                if direction == FrameDirection.DOWNSTREAM:
                    if isinstance(frame, InterruptionFrame):
                        interrupted.set()
                    if isinstance(frame, FunctionCallResultFrame):
                        results.append(frame.result)
                        if isinstance(frame.result, dict) and frame.result.get("status") == "connected":
                            self.assertNotIn("tools/call", server.calls)
                            connected.set()
                    if isinstance(frame, TTSAudioRawFrame):
                        audio.append(len(frame.audio))
                await probe.push_frame(frame, direction)

        context = LLMContext(tools=runtime.tools_schema())
        user, assistant = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
            user_turn_strategies=ExternalUserTurnStrategies(enable_interruptions=False),
        ))
        tts = build_tts_service()
        synthesize = tts.run_tts

        async def record_speech(text, context_id):
            spoken.append(text)
            if "test" in runtime._authorizing:
                waiting.set()
            if live:
                async for frame in synthesize(text, context_id):
                    yield frame
            else:
                yield TTSAudioRawFrame(b"\0" * 4800, 24000, 1, context_id=context_id)
            if connected.is_set() and server.calls.count("tools/call"):
                completed.set()

        tts.run_tts = record_speech

        @llm.event_handler("on_function_calls_cancelled")
        async def on_cancel(service, calls):
            cancelled.set()

        worker = PipelineWorker(Pipeline([user, llm, Probe(), tts, Probe(), assistant]),
                                enable_rtvi=False, idle_timeout_secs=None, app_resources=runtime)
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)
        running = asyncio.create_task(runner.run())
        try:
            async with asyncio.timeout(120 if live else 10):
                await worker.queue_frame(LLMMessagesAppendFrame(
                    [{"role": "user", "content": "Read the private value from the test integration."}], run_llm=True))
                await waiting.wait()
                await worker.queue_frame(InterruptionFrame())
                await interrupted.wait()
                self.assertIn("test", runtime._authorizing)
                if cancel:
                    await worker.queue_frame(LLMMessagesAppendFrame(
                        [{"role": "user", "content": "Cancel login."}], run_llm=True))
                    await cancelled.wait()
                    self.assertFalse(runtime._authorizing)
                    self.assertFalse(connected.is_set())
                    self.assertNotIn("tools/call", server.calls)
                else:
                    release.set()
                    await completed.wait()
                    self.assertEqual(server.calls.count("tools/call"), 1)
                    self.assertIn("test__read_value", [t.name for t in context.tools.standard_tools])
                    self.assertGreater(sum(audio), 0)
                speech = " ".join(spoken)
                for private in ("auth_required", "awaiting_authorization", "structured_content", "access_token", "async_tool"):
                    self.assertNotIn(private, speech)
                prompt = json.dumps(context.get_messages()) + json.dumps(requests)
                self.assertNotIn("token-0", prompt)
                self.assertNotIn("refresh-1", prompt)
        finally:
            await worker.cancel()
            await asyncio.wait_for(running, 10)

    async def test_completion_runs_inference_without_user_message_or_action_replay(self):
        await self.run_flow()

    async def test_interruption_keeps_login_and_explicit_cancel_stops_it(self):
        await self.run_flow(cancel=True)

    @unittest.skipUnless(os.environ.get("LUNA_LIVE_TESTS") == "1", "Requires local Ollama/Qwen and VoiceStudio")
    async def test_live_qwen_and_sentence_tts(self):
        await self.run_flow(live=True)
