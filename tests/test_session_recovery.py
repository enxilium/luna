"""Real voice pipeline under socket, setup, tool-result and renewal failures."""

import asyncio
from contextlib import asynccontextmanager
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from google.genai import errors, types
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.workers.runner import WorkerRunner

from src.agent import conversation
from src.agent.services import build_llm_service
from src.audio.wake_word import WakeWordGate
from src.integrations.mcp import MCPRuntime
from tests.test_mcp_pipeline import FakeGemini, FakeSession
from tests.test_turn_flow import Passthrough


WAKE = b"\x01\0" * 320
SPEECH = b"\x02\0" * 320


class FaultSession(FakeSession):
    async def receive(self):
        while (message := await self.messages.get()) is not None:
            if isinstance(message, Exception):
                raise message
            yield message

    async def send_realtime_input(self, **kwargs):
        if self.server.audio_error:
            self.server.audio_error = False
            raise ConnectionError("simulated send failure")
        await super().send_realtime_input(**kwargs)

    async def send_tool_response(self, **kwargs):
        if self.server.result_error:
            self.server.result_error = False
            raise ConnectionError("simulated tool-result send failure")
        await super().send_tool_response(**kwargs)

    async def send_client_content(self, **kwargs):
        await super().send_client_content(**kwargs)
        if self.server.block_seed and len(self.server.sessions) == 2:
            self.server.seeding.set()
            await self.server.seed_release.wait()


class FaultServer(FakeGemini):
    def __init__(self):
        super().__init__()
        self.attempts = []
        self.failures = []
        self.offline = False
        self.audio_error = self.result_error = False
        self.block_attempt = None
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.block_seed = False
        self.seeding = asyncio.Event()
        self.seed_release = asyncio.Event()

    @asynccontextmanager
    async def connect(self, *, model, config):
        self.attempts.append(config.model_copy(deep=True))
        if len(self.attempts) == self.block_attempt:
            self.blocked.set()
            await self.release.wait()
        if self.offline:
            raise ConnectionError("simulated offline network")
        if self.failures:
            failure = self.failures.pop(0)
            if failure:
                raise failure
        session = FaultSession(self, types.LiveConnectConfig.model_validate(
            config.model_dump(exclude_none=True),
        ))
        self.sessions.append(session)
        try:
            yield session
        finally:
            await session.close()


class SessionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.001)

    async def start(self, server=None, tools=(), recovery_timeout=0.15):
        server = server or FaultServer()
        runtime = MCPRuntime([])
        self.addAsyncCleanup(runtime.close)
        with (
            patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}),
            patch.object(runtime, "tools_schema", side_effect=lambda: ToolsSchema(tools)),
        ):
            llm = build_llm_service(runtime, defer_connection=True)
        llm._connect_timeout = 0.04
        llm._recovery_timeout = recovery_timeout
        llm._retry_delay = 0.001
        llm._client.aio.live.connect = server.connect
        detector = SimpleNamespace(
            process=Mock(side_effect=lambda pcm, rate: ["Luna"] if pcm == WAKE else []),
            reset=Mock(),
        )
        frames, gates, activities = [], [], []
        transport = SimpleNamespace(
            input=lambda: Passthrough(frames), output=lambda: Passthrough(frames),
        )

        def gate(*args):
            result = WakeWordGate(*args)
            gates.append(result)
            return result

        with (
            patch.object(conversation, "build_llm_service", return_value=llm),
            patch.object(conversation, "SherpaDetector", return_value=detector),
            patch.object(conversation, "WakeWordGate", side_effect=gate),
        ):
            worker = conversation.build_worker(transport, runtime, on_activity=activities.append)
        started = asyncio.Event()
        pipeline_errors = []

        @worker.event_handler("on_pipeline_started")
        async def on_started(worker, frame):
            started.set()

        @worker.event_handler("on_pipeline_error")
        async def on_error(worker, frame):
            pipeline_errors.append(frame.error)

        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)
        running = asyncio.create_task(runner.run())

        async def stop():
            await worker.cancel()
            await asyncio.wait_for(running, 3)
            self.assertFalse(any(
                task.get_name().startswith(f"{llm.name}::")
                for task in asyncio.all_tasks() if not task.done()
            ))

        self.addAsyncCleanup(stop)
        await asyncio.wait_for(started.wait(), 2)
        return SimpleNamespace(
            llm=llm, server=server, worker=worker, running=running, gate=gates[0],
            errors=pipeline_errors, runtime=runtime, activities=activities,
        )

    async def wake(self, app):
        await app.worker.queue_frame(InputAudioRawFrame(WAKE, 16000, 1))
        await self.wait_for(lambda: app.llm.ready.is_set() and app.gate._state == "active")
        await self.wait_for(lambda: app.server.sessions[-1].audio)

    async def resumable(self, app):
        await app.server.sessions[-1].messages.put(types.LiveServerMessage(
            session_resumption_update=types.LiveServerSessionResumptionUpdate(
                resumable=True, new_handle="saved-handle",
            ),
        ))
        await self.wait_for(lambda: app.llm._session_resumption_handle == "saved-handle")

    async def disconnect(self, app):
        count = len(app.server.sessions)
        await app.server.sessions[-1].messages.put(ConnectionError("simulated socket close"))
        await self.wait_for(lambda: len(app.server.sessions) > count and app.llm.ready.is_set())

    async def test_failed_initial_handshakes_retry_and_preserve_the_wake(self):
        server = FaultServer()
        server.failures = [ConnectionError(), TimeoutError()]
        app = await self.start(server)
        await self.wake(app)
        self.assertEqual(len(server.attempts), 3)
        self.assertEqual(server.sessions[0].audio[0].data, WAKE)
        self.assertEqual(app.errors, [])

    async def test_hung_setup_has_a_deadline_then_retries(self):
        server = FaultServer()
        server.block_attempt = 1
        app = await self.start(server)
        await self.wake(app)
        self.assertEqual(len(server.attempts), 2)
        self.assertEqual(app.errors, [])

    async def test_offline_budget_rearms_and_the_next_wake_can_connect(self):
        server = FaultServer()
        server.offline = True
        app = await self.start(server, recovery_timeout=0.035)
        await app.worker.queue_frame(InputAudioRawFrame(WAKE, 16000, 1))
        await self.wait_for(lambda: len(server.attempts) > 1 and app.gate._state == "waiting")
        attempts = len(server.attempts)
        await asyncio.sleep(0.02)
        self.assertEqual(len(server.attempts), attempts)
        self.assertFalse(app.running.done())
        self.assertFalse(app.llm.ready.is_set())
        self.assertFalse(app.activities[-1])
        server.offline = False
        await self.wake(app)
        self.assertEqual(server.sessions[-1].audio[0].data, WAKE)
        self.assertTrue(app.activities[-1])
        self.assertEqual(app.activities.count(True), 2)
        self.assertEqual(app.errors, [])

    async def test_auth_rejection_returns_to_wake_without_a_retry_loop(self):
        server = FaultServer()
        server.failures = [errors.ClientError(403, {"message": "permission denied"})]
        app = await self.start(server)
        await app.worker.queue_frame(InputAudioRawFrame(WAKE, 16000, 1))
        await self.wait_for(lambda: server.attempts and app.gate._state == "waiting")
        self.assertEqual(len(server.attempts), 1)
        await self.wake(app)
        self.assertFalse(app.running.done())

    async def test_receive_errors_recover_beyond_the_old_three_failure_limit(self):
        app = await self.start()
        await self.wake(app)
        await self.resumable(app)
        for _ in range(5):
            await self.disconnect(app)
            self.assertEqual(app.server.attempts[-1].session_resumption.handle, "saved-handle")
        self.assertEqual(len(app.server.sessions), 6)
        self.assertEqual(app.activities, [True])
        self.assertFalse(app.running.done())
        self.assertEqual(app.errors, [])

    async def test_failed_send_and_reconnect_buffer_speech_in_order(self):
        app = await self.start()
        await self.wake(app)
        app.server.block_attempt = 2
        app.server.audio_error = True
        await app.worker.queue_frame(InputAudioRawFrame(SPEECH, 16000, 1))
        await asyncio.wait_for(app.server.blocked.wait(), 2)
        await self.wait_for(lambda: app.gate._state == "connecting")
        await app.worker.queue_frame(InputAudioRawFrame(SPEECH, 16000, 1))
        await self.wait_for(lambda: app.gate._buffer == SPEECH)
        app.server.release.set()
        await self.wait_for(lambda: len(app.server.sessions) == 2 and app.server.sessions[-1].audio)
        self.assertEqual(app.server.sessions[-1].audio[0].data, SPEECH)
        await self.wait_for(lambda: sum(len(a.data) for a in app.server.sessions[-1].audio) == 2 * len(SPEECH))
        self.assertFalse(app.running.done())
        self.assertEqual(app.errors, [])

    async def test_rejected_resume_retries_fresh_with_history(self):
        app = await self.start()
        await self.wake(app)
        app.llm.context.add_message({"role": "user", "content": "Remember this context."})
        await self.resumable(app)
        app.server.failures = [errors.ClientError(1007, {"message": "invalid handle"})]
        await self.disconnect(app)
        self.assertEqual(len(app.server.attempts), 3)
        self.assertEqual(app.server.attempts[-2].session_resumption.handle, "saved-handle")
        self.assertIsNone(app.server.attempts[-1].session_resumption.handle)
        self.assertIn("Remember this context", str(app.server.sessions[-1].seeds))

    async def test_expired_handle_is_not_sent(self):
        app = await self.start()
        app.llm._session_resumption_handle = "expired"
        app.llm._resume_expires_at = time.time() - 1
        await self.wake(app)
        self.assertIsNone(app.server.attempts[0].session_resumption.handle)

    async def test_nonresumable_update_discards_the_previous_checkpoint(self):
        app = await self.start()
        await self.wake(app)
        await self.resumable(app)
        await app.server.sessions[-1].messages.put(types.LiveServerMessage(
            session_resumption_update=types.LiveServerSessionResumptionUpdate(resumable=False),
        ))
        await self.wait_for(lambda: app.llm._session_resumption_handle is None)
        await self.disconnect(app)
        self.assertIsNone(app.server.attempts[-1].session_resumption.handle)

    async def test_goaway_renews_without_waiting_for_a_socket_error(self):
        app = await self.start()
        await self.wake(app)
        await self.resumable(app)
        old = app.server.sessions[-1]
        await old.messages.put(types.LiveServerMessage(
            go_away=types.LiveServerGoAway(time_left="1.05s"),
        ))
        await self.wait_for(lambda: len(app.server.sessions) == 2 and app.llm.ready.is_set())
        self.assertTrue(old.closed)
        self.assertEqual(app.server.attempts[-1].session_resumption.handle, "saved-handle")

    async def test_goaway_waits_for_an_inflight_tool_without_replaying_it(self):
        started, release = asyncio.Event(), asyncio.Event()
        effects = Mock()

        async def action(params):
            started.set()
            await release.wait()
            effects()
            await params.result_callback({"status": "completed"})

        app = await self.start(tools=[FunctionSchema("action", "An action", {}, [], action)])
        await self.wake(app)
        await app.server.sessions[-1].call("action")
        await asyncio.wait_for(started.wait(), 2)
        await app.server.sessions[-1].messages.put(types.LiveServerMessage(
            go_away=types.LiveServerGoAway(time_left="2s"),
        ))
        await asyncio.sleep(0.02)
        self.assertEqual(len(app.server.sessions), 1)
        release.set()
        await app.server.sessions[-1].say("Action completed.")
        await self.wait_for(lambda: len(app.server.sessions) == 2 and app.llm.ready.is_set())
        effects.assert_called_once()

    async def test_tool_result_send_failure_retries_the_result_without_the_action(self):
        effects = Mock()

        async def action(params):
            effects()
            await params.result_callback({"status": "completed"})

        app = await self.start(tools=[FunctionSchema("action", "An action", {}, [], action)])
        await self.wake(app)
        await self.resumable(app)
        app.server.result_error = True
        await app.server.sessions[-1].call("action")
        await self.wait_for(lambda: len(app.server.sessions) == 2 and app.server.sessions[-1].responses)
        effects.assert_called_once()
        self.assertEqual(app.server.sessions[-1].responses[0].response, {"status": "completed"})
        self.assertEqual(app.llm._pending_results, {})

    async def test_duplicate_call_after_resumption_never_runs_the_action_twice(self):
        started, release = asyncio.Event(), asyncio.Event()
        effects = Mock()

        async def action(params):
            effects()
            started.set()
            await release.wait()
            await params.result_callback({"status": "completed"})

        app = await self.start(tools=[FunctionSchema("action", "An action", {}, [], action)])
        await self.wake(app)
        await self.resumable(app)
        await app.server.sessions[-1].call("action")
        await asyncio.wait_for(started.wait(), 2)
        await self.disconnect(app)
        duplicate = types.LiveServerMessage(tool_call=types.LiveServerToolCall(
            function_calls=[types.FunctionCall(id="call-1", name="action", args={})],
        ))
        await app.server.sessions[-1].messages.put(duplicate)
        release.set()
        await self.wait_for(lambda: app.server.sessions[-1].responses)
        await app.server.sessions[-1].messages.put(duplicate)
        await self.wait_for(lambda: len(app.server.sessions[-1].responses) == 2)
        effects.assert_called_once()

    async def test_end_during_reconnect_cancels_retries_and_rearms(self):
        app = await self.start()
        await self.wake(app)
        app.server.offline = True
        await app.server.sessions[-1].messages.put(ConnectionError())
        await self.wait_for(lambda: len(app.server.attempts) > 1)
        await app.llm._end_conversation()
        attempts = len(app.server.attempts)
        await asyncio.sleep(0.02)
        self.assertEqual(len(app.server.attempts), attempts)
        self.assertEqual(app.gate._state, "waiting")
        self.assertFalse(app.llm.ready.is_set())
        app.server.offline = False
        await self.wake(app)

    async def test_silent_end_waits_for_the_final_turn(self):
        app = await self.start()
        await self.wake(app)
        session = app.server.sessions[-1]
        await session.call("end_conversation")
        await self.wait_for(lambda: session.responses)
        self.assertFalse(session.closed)
        await session.messages.put(types.LiveServerMessage(server_content=types.LiveServerContent(
            turn_complete=True, interaction_status="IN_PROGRESS",
        )))
        await self.wait_for(lambda: app.llm._turn_complete_pending_idle is not None)
        self.assertFalse(session.closed)
        await session.messages.put(types.LiveServerMessage(server_content=types.LiveServerContent(
            turn_complete=True,
        )))
        await self.wait_for(lambda: session.closed and app.gate._state == "waiting")
        self.assertFalse(app.llm._end_requested)
        await self.wake(app)
        await app.server.sessions[-1].say("A new conversation.")
        await asyncio.sleep(0.02)
        self.assertTrue(app.llm.ready.is_set())

    async def test_interruption_cancels_an_end_waiting_for_playback(self):
        app = await self.start()
        await self.wake(app)
        session = app.server.sessions[-1]
        flushing, release = asyncio.Event(), asyncio.Event()

        async def flush():
            flushing.set()
            await release.wait()
            return True

        with patch.object(app.worker, "flush_pipeline", side_effect=flush):
            await session.call("end_conversation")
            await self.wait_for(lambda: session.responses)
            await session.say("No problem.")
            await asyncio.wait_for(flushing.wait(), 2)
            await session.messages.put(types.LiveServerMessage(server_content=types.LiveServerContent(
                interrupted=True,
            )))
            await self.wait_for(lambda: app.llm._end_task is None)
            self.assertFalse(app.llm._end_requested)
            release.set()
            await session.say("What would you like instead?")
            await asyncio.sleep(0.02)
            self.assertFalse(session.closed)
            self.assertTrue(app.llm.ready.is_set())

    async def test_reconnect_clears_a_pending_end(self):
        app = await self.start()
        await self.wake(app)
        await app.server.sessions[-1].call("end_conversation")
        await self.wait_for(lambda: app.server.sessions[-1].responses)
        await self.disconnect(app)
        self.assertFalse(app.llm._end_requested)
        self.assertIsNone(app.llm._end_task)
        await app.server.sessions[-1].say("Still connected.")
        await asyncio.sleep(0.02)
        self.assertTrue(app.llm.ready.is_set())

    async def test_shutdown_during_a_hung_connection_never_reconnects(self):
        server = FaultServer()
        server.block_attempt = 1
        app = await self.start(server)
        await app.worker.queue_frame(InputAudioRawFrame(WAKE, 16000, 1))
        await asyncio.wait_for(server.blocked.wait(), 2)
        await app.worker.cancel()
        await asyncio.wait_for(app.running, 2)
        server.release.set()
        await asyncio.sleep(0.02)
        self.assertEqual(len(server.attempts), 1)

    async def test_catalog_refresh_can_retry_its_handshake(self):
        app = await self.start()
        await self.wake(app)
        app.server.failures = [ConnectionError()]
        app.runtime.catalog_revision += 1
        await app.worker.queue_frame(InputAudioRawFrame(SPEECH, 16000, 1))
        await self.wait_for(lambda: len(app.server.sessions) == 2 and app.server.sessions[-1].audio)
        self.assertEqual(len(app.server.attempts), 3)
        self.assertIn("end_conversation", app.server.sessions[-1].functions)
        self.assertEqual(b"".join(a.data for a in app.server.sessions[-1].audio), SPEECH)
        self.assertEqual(app.errors, [])

    async def test_fresh_session_seeds_recent_whole_turns_without_trimming_local_history(self):
        app = await self.start()
        for _ in range(12):
            app.llm.context.add_message({"role": "user", "content": "old request " + "x" * 8000})
            app.llm.context.add_message({"role": "assistant", "content": "old response " + "y" * 8000})
        app.llm.context.add_message({"role": "user", "content": "Latest request"})
        app.llm.context.add_message({"role": "assistant", "content": "Latest response"})
        await self.wake(app)
        seeded = app.server.sessions[-1].seeds[0]
        self.assertIn("Latest request", str(seeded))
        self.assertIn("Latest response", str(seeded))
        self.assertLess(len(str(seeded)), 34000)
        self.assertEqual(len(app.llm.context.get_messages()), 26)

    async def test_tool_finishing_during_fresh_history_seed_is_still_delivered(self):
        started, finish = asyncio.Event(), asyncio.Event()
        effects = Mock()

        async def action(params):
            started.set()
            await finish.wait()
            effects()
            await params.result_callback({"status": "completed", "value": 42})

        app = await self.start(tools=[FunctionSchema("action", "An action", {}, [], action)],
                               recovery_timeout=0.5)
        app.llm._connect_timeout = 0.4
        await self.wake(app)
        await app.server.sessions[-1].call("action")
        await asyncio.wait_for(started.wait(), 2)
        app.server.block_seed = True
        await app.server.sessions[-1].messages.put(ConnectionError())
        await asyncio.wait_for(app.server.seeding.wait(), 2)
        finish.set()
        await self.wait_for(lambda: any(
            isinstance(m, dict) and m.get("role") == "tool" and '42' in str(m.get("content"))
            for m in app.llm.context.get_messages()
        ))
        app.server.seed_release.set()
        await self.wait_for(lambda: app.llm.ready.is_set())
        self.assertIn("returned", str(app.server.sessions[-1].seeds[-1]))
        self.assertIn("42", str(app.server.sessions[-1].seeds[-1]))
        effects.assert_called_once()

    async def test_wake_deadline_includes_slow_prerequisites_and_rearms(self):
        app = await self.start(recovery_timeout=0.025)

        async def slow_prerequisite():
            await asyncio.Event().wait()

        with patch.object(app.llm, '_load_memory', side_effect=slow_prerequisite):
            await app.worker.queue_frame(InputAudioRawFrame(WAKE, 16000, 1))
            await self.wait_for(lambda: app.gate._state == 'connecting')
            await self.wait_for(lambda: app.gate._state == 'waiting')
        self.assertEqual(app.server.attempts, [])
        self.assertFalse(app.running.done())
        await self.wake(app)

    async def test_ending_discards_unsent_audio_and_old_pending_results(self):
        app = await self.start()
        await self.wake(app)
        app.llm._unsent_audio.extend(SPEECH)
        app.llm._pending_results['old'] = ('action', {'status': 'completed'})
        app.llm._tool_results['old'] = ('action', {'status': 'completed'})
        await app.llm._end_conversation()
        self.assertEqual(app.llm._unsent_audio, b'')
        self.assertEqual(app.llm._pending_results, {})
        self.assertEqual(app.llm._tool_results, {})
        await self.wake(app)
        self.assertEqual(app.server.sessions[-1].audio[0].data, WAKE)

    async def test_goaway_waits_for_a_turn_boundary_before_early_renewal(self):
        app = await self.start()
        await self.wake(app)
        session = app.server.sessions[-1]
        await session.messages.put(types.LiveServerMessage(
            go_away=types.LiveServerGoAway(time_left="50s"),
        ))
        await asyncio.sleep(0.02)
        self.assertEqual(len(app.server.sessions), 1)
        await session.say("Finished this turn.")
        await self.wait_for(lambda: len(app.server.sessions) == 2 and app.llm.ready.is_set())
