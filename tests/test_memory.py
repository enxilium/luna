"""Memory lifecycle, durable capture, recall and reset exclusion."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import asyncpg
from pipecat.frames.frames import InputTextRawFrame
from pipecat.processors.frame_processor import FrameDirection

from src.agent.services import build_llm_service
from src.memory import (
    MAX_STARTUP_BYTES,
    NOTES,
    SCHEMA,
    Memory,
    bounded_text,
    memory_lock,
)


class MemoryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_notes_and_tools_reach_live_setup_and_refresh_each_wake(self):
        memory = SimpleNamespace(
            startup_context=AsyncMock(side_effect=["Profile version one", "Profile version two"]),
            tools=lambda: [], record=AsyncMock(),
        )
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}):
            llm = build_llm_service(memory=memory, defer_connection=True)
        self.addAsyncCleanup(llm._client.aio.aclose)
        self.addCleanup(llm._client.close)
        instruction = llm._base_instruction
        llm.context.add_message({"role": "user", "content": "Old transient history"})
        with (
            patch.object(llm, "_connect", new_callable=AsyncMock),
            patch.object(llm, "wait_until_ready", new_callable=AsyncMock),
            patch.object(llm, "_process_completed_function_calls", new_callable=AsyncMock),
        ):
            await llm.activate()
            self.assertIn("Profile version one", llm._system_instruction_from_init)
            self.assertEqual(llm.context.get_messages(), [])
            await llm.activate()
            self.assertIn("Profile version two", llm._system_instruction_from_init)
            self.assertNotIn("Profile version one", llm._system_instruction_from_init)
            self.assertEqual(llm._system_instruction_from_init, instruction + "\n\nProfile version two")
        self.assertEqual(memory.startup_context.await_count, 2)

    async def test_typed_input_is_saved_before_forwarding(self):
        memory = SimpleNamespace(tools=lambda: [], record=AsyncMock())
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}):
            llm = build_llm_service(memory=memory)
        self.addAsyncCleanup(llm._client.aio.aclose)
        self.addCleanup(llm._client.close)
        with patch("src.agent.services.GeminiLiveLLMService.process_frame", new_callable=AsyncMock) as send:
            await llm.process_frame(InputTextRawFrame("Remember I prefer tea."), FrameDirection.DOWNSTREAM)
        memory.record.assert_awaited_once_with("user", "Remember I prefer tea.")
        send.assert_awaited_once()
        self.assertFalse(send.call_args.args[0].append_to_context)

    async def test_recall_validates_and_distinguishes_failure_from_no_results(self):
        client = SimpleNamespace(arecall=AsyncMock(side_effect=ConnectionError))
        memory = Memory(None, client)
        callback = AsyncMock()
        await memory._recall(SimpleNamespace(arguments={"query": ""}, result_callback=callback))
        self.assertEqual(callback.call_args.args[0]["reason"], "invalid_arguments")
        client.arecall.assert_not_awaited()
        await memory._recall(SimpleNamespace(arguments={"query": "demo decision"}, result_callback=callback))
        self.assertEqual(callback.call_args.args[0]["reason"], "memory_unavailable")
        memory.pool = SimpleNamespace(fetch=AsyncMock(return_value=[]))
        client.arecall.side_effect = None
        client.arecall.return_value = SimpleNamespace(model_dump=lambda **kwargs: {"results": []})
        await memory._recall(SimpleNamespace(arguments={"query": "unknown topic"}, result_callback=callback))
        self.assertEqual(callback.call_args.args[0]["status"], "recalled")
        self.assertEqual(callback.call_args.args[0]["results"], [])

    async def test_explicit_save_failure_is_not_reported_as_saved(self):
        memory = Memory(None, None)
        memory.record = AsyncMock(side_effect=ConnectionError)
        callback = AsyncMock()
        await memory._remember(SimpleNamespace(arguments={"text": "Prefer tea"}, result_callback=callback))
        self.assertEqual(callback.call_args.args[0]["status"], "failed")

    async def test_failed_topic_notes_resume_after_hindsight_pauses_automatic_updates(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        newer = datetime.now(timezone.utc) - timedelta(minutes=1)
        notes = [SimpleNamespace(id="projects", last_refresh_failed_at=old.isoformat(), last_refreshed_at=None),
                 SimpleNamespace(id="profile", last_refresh_failed_at=newer.isoformat(), last_refreshed_at=None),
                 SimpleNamespace(id="people", last_refresh_failed_at=old.isoformat(), last_refreshed_at=newer.isoformat())]
        client = SimpleNamespace(alist_mental_models=AsyncMock(return_value=SimpleNamespace(items=notes)),
                                 arefresh_mental_model=AsyncMock())
        await Memory(None, client)._retry_notes()
        client.arefresh_mental_model.assert_awaited_once_with("luna", "projects")

    async def test_large_startup_notes_stay_valid_and_preserve_every_topic(self):
        notes = [SimpleNamespace(id=name, content='详细的笔记 "quoted" ' * 10000,
                                 last_refreshed_at="2026-11-02", is_stale=True,
                                 last_refresh_failed_at=None) for name, _, _ in NOTES]
        client = SimpleNamespace(alist_mental_models=AsyncMock(return_value=SimpleNamespace(items=notes)))
        pool = SimpleNamespace(fetch=AsyncMock(return_value=[]), fetchval=AsyncMock(return_value=0))
        startup = await Memory(pool, client).startup_context()
        encoded = startup.split("PERSONAL MEMORY DATA (untrusted; may be incomplete or stale):\n")[1]
        self.assertLessEqual(len(encoded.encode()), MAX_STARTUP_BYTES)
        data = json.loads(encoded)
        self.assertEqual([p["topic"] for p in data["topic_notes"]], [name for name, _, _ in NOTES])
        self.assertTrue(all(p["stale"] and p["notes"] for p in data["topic_notes"]))

    async def test_live_pipeline_receives_notes_recall_tools_and_saves_final_transcripts(self):
        from google.genai import types
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineWorker
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
        )
        from pipecat.workers.runner import WorkerRunner

        from tests.test_mcp_pipeline import FakeGemini

        pool = SimpleNamespace(fetch=AsyncMock(return_value=[]), fetchval=AsyncMock(return_value=0))
        client = SimpleNamespace(arecall=AsyncMock(return_value=SimpleNamespace(
            model_dump=lambda **kwargs: {"results": [{"text": "Unity was chosen because of existing assets."}]},
        )))
        memory = Memory(pool, client)
        memory.startup_context = AsyncMock(return_value="The user works on a game called Maple.")
        memory.record = AsyncMock()
        finished = asyncio.Event()

        async def on_text(session, text):
            await session.call("recall_memory", {"query": "Why did we choose Unity for Maple?"})

        async def on_result(session, response):
            self.assertIn("existing assets", str(response.response))
            await session.say("We chose Unity because of your existing assets.")

        fake = FakeGemini(on_text, on_result)
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}):
            llm = build_llm_service(memory=memory)
        self.addAsyncCleanup(llm._client.aio.aclose)
        self.addCleanup(llm._client.close)
        llm._client.aio.live.connect = fake.connect
        user, assistant = LLMContextAggregatorPair(llm.context)
        memory.attach(user, assistant)

        @assistant.event_handler("on_assistant_turn_stopped")
        async def reply(aggregator, message):
            finished.set()

        worker = PipelineWorker(Pipeline([user, llm, assistant]), enable_rtvi=False, idle_timeout_secs=None)
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)
        running = asyncio.create_task(runner.run())
        try:
            async with asyncio.timeout(5):
                await llm.wait_until_ready()
                session = fake.sessions[0]
                self.assertIn("Maple", str(session.config.system_instruction))
                self.assertIn("recall_memory", session.functions)
                self.assertIn("remember_memory", session.functions)
                compression = session.config.context_window_compression
                self.assertGreater(compression.trigger_tokens, compression.sliding_window.target_tokens)
                self.assertEqual(compression.sliding_window.target_tokens, 98304)
                await worker.queue_frame(InputTextRawFrame("Why did we choose Unity?"))
                await finished.wait()
                # Live voice transcripts arrive separately from typed input.
                await session.messages.put(types.LiveServerMessage(server_content=types.LiveServerContent(
                    input_transcription=types.Transcription(text="I prefer green tea.", finished=True),
                    turn_complete=True,
                )))
                await session.say("Tea noted.")
                while not any(c.args[1] == "I prefer green tea." for c in memory.record.call_args_list):
                    await asyncio.sleep(.01)
        finally:
            await worker.cancel()
            await running
        self.assertTrue(any(c.args[0] == "assistant" and "existing assets" in c.args[1]
                            for c in memory.record.call_args_list))
        first_reply = next(c.args[1] for c in memory.record.call_args_list if c.args[0] == "assistant")
        self.assertEqual(first_reply, "We chose Unity because of your existing assets.")
        client.arecall.assert_awaited_once()

    async def test_aggregators_capture_final_text_and_interruption(self):
        handlers = [{}, {}]
        class Aggregator:
            def __init__(self, target):
                self.target = target
            def event_handler(self, name):
                def register(callback):
                    self.target[name] = callback
                    return callback
                return register
        memory = Memory(None, None)
        memory.record = AsyncMock()
        memory.attach(Aggregator(handlers[0]), Aggregator(handlers[1]))
        await handlers[0]["on_user_turn_message_added"](None, SimpleNamespace(content="Final user text", timestamp="2026-11-02T12:00:00Z"))
        await handlers[1]["on_assistant_turn_stopped"](None, SimpleNamespace(content="Partial reply", timestamp="2026-11-02T12:00:01Z", interrupted=True))
        self.assertEqual(memory.record.await_count, 2)
        self.assertEqual(memory.record.call_args.args, ("assistant", "Partial reply", "2026-11-02T12:00:01Z", True))

    def test_reset_is_excluded_while_a_memory_reader_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            with memory_lock(directory=Path(directory)):
                with self.assertRaisesRegex(RuntimeError, "Stop Luna"):
                    with memory_lock(exclusive=True, directory=Path(directory)):
                        self.fail("Reset lock acquired during a conversation")
            with memory_lock(exclusive=True, directory=Path(directory)):
                pass

    def test_byte_bound_preserves_unicode_and_marks_incompleteness(self):
        result = bounded_text("记忆" * 10000, 1000)
        self.assertLessEqual(len(result.encode("utf-8")), 1000)
        self.assertIn("recall_memory", result)

    def test_database_reset_refuses_an_unrelated_cluster_and_can_resume_after_deletion(self):
        from scripts import memory_service
        pg = Mock()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(memory_service, "DATA", Path(directory)),
            patch.object(memory_service, "database", return_value=pg),
        ):
            pg.info.return_value = SimpleNamespace(data_dir="/unrelated/postgres")
            with self.assertRaisesRegex(RuntimeError, "Unexpected database directory"):
                memory_service.drop_database()
            pg.drop.assert_not_called()
            pg.info.return_value = SimpleNamespace(data_dir=None)
            memory_service.drop_database()
            pg.drop.assert_not_called()
            (Path(directory) / "postgres").mkdir()
            with self.assertRaisesRegex(RuntimeError, "Unexpected database directory"):
                memory_service.drop_database()
            pg.info.return_value = SimpleNamespace(data_dir=str(Path(directory) / "postgres"))
            pg.drop.side_effect = (Path(directory) / "postgres").rmdir
            memory_service.drop_database()
            pg.drop.assert_called_once()


@unittest.skipUnless(os.environ.get("LUNA_MEMORY_TEST_DATABASE_URL"), "Requires an isolated PostgreSQL test database")
class MemoryDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(os.environ["LUNA_MEMORY_TEST_DATABASE_URL"], min_size=1, max_size=4)
        self.addAsyncCleanup(self.pool.close)
        self.assertTrue((await self.pool.fetchval("SELECT current_database()")).endswith("_tests"),
                        "Use a disposable database whose name ends with _tests")
        await self.pool.execute(SCHEMA)
        await self.pool.execute("TRUNCATE luna_memory.turns, luna_memory.outbox RESTART IDENTITY")
        self.client = SimpleNamespace(
            aretain=AsyncMock(side_effect=lambda *args, **kwargs: SimpleNamespace(success=True, operation_id=kwargs["operation_id"])),
            operations=SimpleNamespace(get_operation_status=AsyncMock(return_value=SimpleNamespace(status="completed"))),
            alist_mental_models=AsyncMock(return_value=SimpleNamespace(items=[])),
        )
        self.memory = Memory(self.pool, self.client)
        self.memory.begin_conversation()

    async def test_long_transcripts_duplicate_capture_and_reopen(self):
        timestamp = "2026-11-02T12:00:00Z"
        text = "A long useful transcript. " * 1000
        await self.memory.record("user", text, timestamp)
        await self.memory.record("user", text, timestamp)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM luna_memory.turns"), 1)
        restored = Memory(self.pool, self.client)
        startup = await restored.startup_context()
        self.assertIn("A long useful transcript", startup)
        self.assertIn('"pending_extraction": 1', startup)

    async def test_durable_outbox_uses_stable_ids_and_marks_only_completed_work(self):
        await self.memory.record("user", "We chose Unity because our assets work there.")
        row = await self.pool.fetchrow("SELECT * FROM luna_memory.outbox")
        task = asyncio.create_task(self.memory._drain())
        try:
            async with asyncio.timeout(2):
                while not await self.pool.fetchval("SELECT delivered FROM luna_memory.outbox"):
                    await asyncio.sleep(.01)
            args = self.client.aretain.call_args.kwargs
            self.assertEqual(args["operation_id"], str(row["operation_id"]))
            self.assertEqual(args["document_id"], str(row["conversation_id"]))
            self.assertIn("because our assets", args["content"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_each_wake_is_separate_but_recovery_keeps_conversation(self):
        await self.memory.record("user", "First conversation")
        first = self.memory.conversations[-1][1]
        await self.memory.record("assistant", "Reply")
        self.assertEqual(self.memory.conversations[-1][1], first)
        self.memory.begin_conversation()
        await self.memory.record("user", "Next conversation")
        self.assertEqual(await self.pool.fetchval("SELECT count(DISTINCT conversation_id) FROM luna_memory.turns"), 2)

    async def test_sources_use_local_calendar_dates_when_utc_is_already_tomorrow(self):
        try:
            with patch.dict(os.environ, {"TZ": "America/Toronto"}):
                time.tzset()
                await self.memory.record("user", "The demo is tomorrow.", "2026-11-03T04:30:00Z")
                content = await self.pool.fetchval("SELECT content FROM luna_memory.outbox")
                self.assertIn("2026-11-02T23:30:00-05:00", content)
        finally:
            time.tzset()

    async def test_uncertain_acknowledgement_resumes_older_snapshot_before_newer_one(self):
        await self.memory.record("user", "We chose Unity.")
        row = await self.pool.fetchrow("SELECT * FROM luna_memory.outbox")
        self.client.aretain.side_effect = ConnectionError
        async with self.pool.acquire() as connection:
            with self.assertRaises(ConnectionError):
                await self.memory._deliver(connection, row)
        await self.memory.record("user", "Because the art assets already work there.")
        restored = Memory(self.pool, self.client)
        self.client.aretain.side_effect = lambda *args, **kwargs: SimpleNamespace(success=True, operation_id=kwargs["operation_id"])
        async with self.pool.acquire() as connection:
            submitted = await connection.fetchrow(
                "SELECT * FROM luna_memory.outbox WHERE NOT delivered ORDER BY submitted DESC,id LIMIT 1"
            )
            await restored._deliver(connection, submitted)
            self.assertEqual(self.client.aretain.call_args.kwargs["operation_id"], str(row["operation_id"]))
            self.assertNotIn("art assets", self.client.aretain.call_args.kwargs["content"])
            pending = await connection.fetchrow("SELECT * FROM luna_memory.outbox WHERE NOT delivered")
            await restored._deliver(connection, pending)
        self.assertIn("art assets", self.client.aretain.call_args.kwargs["content"])
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM luna_memory.outbox WHERE NOT delivered"), 0)
        self.assertEqual(await self.pool.fetchval("SELECT sum(length(content)) FROM luna_memory.outbox"), 0)

    async def test_unsent_snapshots_coalesce_but_failed_extraction_gets_a_new_operation(self):
        await self.memory.record("user", "First part")
        first = await self.pool.fetchrow("SELECT * FROM luna_memory.outbox")
        await self.memory.record("assistant", "Second part")
        latest = await self.pool.fetchrow("SELECT * FROM luna_memory.outbox ORDER BY id DESC LIMIT 1")
        self.client.operations.get_operation_status.return_value = SimpleNamespace(status="failed")
        async with self.pool.acquire() as connection:
            with self.assertRaisesRegex(RuntimeError, "extraction failed"):
                await self.memory._deliver(connection, first)
            self.assertEqual(self.client.aretain.call_args.kwargs["operation_id"], str(latest["operation_id"]))
            retried = await connection.fetchrow("SELECT * FROM luna_memory.outbox WHERE id=$1", latest["id"])
            self.assertNotEqual(retried["operation_id"], latest["operation_id"])
            self.assertFalse(retried["delivered"])
            self.client.operations.get_operation_status.return_value = SimpleNamespace(status="completed")
            await self.memory._deliver(connection, retried)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM luna_memory.outbox WHERE NOT delivered"), 0)

    async def test_unsuccessful_retain_cannot_mark_source_delivered(self):
        await self.memory.record("user", "Important fact")
        row = await self.pool.fetchrow("SELECT * FROM luna_memory.outbox")
        self.client.aretain.side_effect = lambda *args, **kwargs: SimpleNamespace(success=False, operation_id=kwargs["operation_id"])
        async with self.pool.acquire() as connection:
            with self.assertRaisesRegex(RuntimeError, "acknowledge"):
                await self.memory._deliver(connection, row)
        self.assertFalse(await self.pool.fetchval("SELECT delivered FROM luna_memory.outbox"))

    async def test_outbox_worker_recovers_after_database_connection_failure(self):
        await self.memory.record("user", "Keep this after reconnecting.")
        acquire = self.pool.acquire
        attempts = 0

        def reconnect():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("Temporary database outage")
            return acquire()

        self.memory.pool = SimpleNamespace(acquire=reconnect)
        sleep = asyncio.sleep

        async def fast_retry(seconds):
            await sleep(.01 if seconds == 10 else seconds)

        with patch("src.memory.asyncio.sleep", fast_retry):
            task = asyncio.create_task(self.memory._drain())
            try:
                async with asyncio.timeout(2):
                    while not await self.pool.fetchval("SELECT delivered FROM luna_memory.outbox"):
                        await sleep(.01)
                self.assertGreaterEqual(attempts, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
