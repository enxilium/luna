"""Typed chat routing: Flash-Lite/Flash by brain mode, quota fallback, Live transcripts, and no tool replay."""

import asyncio
import queue
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4

from google.genai import errors, types
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

from src.agent.text import BRAIN_MODEL, FAST_MODEL, LIVE_MODEL, TextAgent, TextRequest, step_label
from tests.test_mcp_pipeline import FakeGemini


def quota(delay=None):
    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{delay}s"}] if delay else []
    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": details}})


def chunk(*parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))])


class FakeModels:
    """Scripted generate_content_stream: script[model] is a list of chunk lists or exceptions, consumed in order."""

    def __init__(self, script):
        self.script, self.calls = script, []

    async def generate_content_stream(self, *, model, contents, config):
        self.calls.append((model, list(contents), config))
        step = self.script[model].pop(0)
        if isinstance(step, Exception):
            raise step

        async def stream():
            for item in step:
                yield item

        return stream()


class FakeClient:
    def __init__(self, script, live=None):
        self.models = FakeModels(script)
        self.aio = SimpleNamespace(models=self.models, live=live or FakeGemini(), aclose=self._aclose)
        self.closed = False

    async def _aclose(self):
        self.closed = True

    def close(self):
        pass


class FakeMemory:
    def __init__(self):
        self.records = []

    async def record(self, speaker, text, timestamp=None, interrupted=False, *, conversation_id=None):
        self.records.append((speaker, text, conversation_id))

    async def startup_context(self, *, conversation_id=None):
        return "PERSONAL MEMORY DATA: none"

    def tools(self, *, conversation_id=None):
        return []


class TextAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool_calls = []

        async def count(params):
            self.tool_calls.append(dict(params.arguments))
            await params.result_callback({"status": "ok", "count": len(self.tool_calls)})

        tools = ToolsSchema([FunctionSchema("test__count", "Count once", {"step": {"type": "string"}}, [], count)])
        self.integrations = SimpleNamespace(integrations={}, tools_schema=lambda: tools)
        self.chat_id = str(uuid4())

    def agent(self, script, live=None, memory=None):
        self.client = FakeClient(script, live)
        return TextAgent(self.integrations, memory, client=self.client)

    def request(self, text="Hello", *, brain=False, history=()):
        return TextRequest(self.chat_id, 7, history, text, (), brain)

    async def test_brain_mode_selects_flash_and_default_selects_flash_lite_with_streamed_text(self):
        memory = FakeMemory()
        agent = self.agent({
            FAST_MODEL: [[chunk(types.Part(text="Quick ")), chunk(types.Part(text="answer."))]],
            BRAIN_MODEL: [[chunk(types.Part(text="Deep answer."))]],
        }, memory=memory)
        updates = []
        text, model = await agent.reply(self.request(history=(
            {"role": "user", "text": "Earlier", "files": ()}, {"role": "luna", "text": "Earlier reply", "files": ()},
        )), lambda t, m: updates.append((t, m)))
        self.assertEqual((text, model), ("Quick answer.", FAST_MODEL))
        self.assertEqual(updates, [("Quick", FAST_MODEL), ("Quick answer.", FAST_MODEL)])
        sent = self.client.models.calls[0][1]
        self.assertEqual([c.role for c in sent], ["user", "model", "user"])
        self.assertIn("plain text", self.client.models.calls[0][2].system_instruction)
        self.assertEqual(await agent.reply(self.request(brain=True), lambda *a: None), ("Deep answer.", BRAIN_MODEL))
        conversation = UUID(self.chat_id)
        self.assertEqual(memory.records[:2], [("user", "Hello", conversation), ("assistant", "Quick answer.", conversation)])

    async def test_exhausted_model_falls_back_to_the_other_flash_and_waits_for_its_retry_delay(self):
        agent = self.agent({
            FAST_MODEL: [quota(delay=120)],
            BRAIN_MODEL: [[chunk(types.Part(text="From Flash."))], [chunk(types.Part(text="Still Flash."))]],
        })
        self.assertEqual(await agent.reply(self.request(), lambda *a: None), ("From Flash.", BRAIN_MODEL))
        self.assertEqual(await agent.reply(self.request(), lambda *a: None), ("Still Flash.", BRAIN_MODEL))
        self.assertEqual([call[0] for call in self.client.models.calls], [FAST_MODEL, BRAIN_MODEL, BRAIN_MODEL])

    async def test_quota_after_a_tool_call_continues_on_the_next_model_without_replaying_the_tool(self):
        call = types.Part(function_call=types.FunctionCall(id="c1", name="test__count", args={"step": "one"}),
                          thought_signature=b"lite-signature")
        agent = self.agent({
            FAST_MODEL: [[chunk(types.Part(text="Checking."), call)], quota()],
            BRAIN_MODEL: [[chunk(types.Part(text="Counted once."))]],
        })
        steps = []
        text, model = await agent.reply(self.request(), lambda *a: None, steps.append)
        self.assertEqual((text, model), ("Checking.\n\nCounted once.", BRAIN_MODEL))
        self.assertEqual(self.tool_calls, [{"step": "one"}])
        self.assertEqual(steps, [("Test", "Count")])
        retried = self.client.models.calls[1][1]
        self.assertEqual(retried[-1].parts[0].function_response.response, {"status": "ok", "count": 1})
        handed_over = self.client.models.calls[2][1]
        self.assertFalse(any(p.function_call or p.function_response or p.thought_signature
                             for c in handed_over for p in c.parts))
        self.assertIn('[Function test__count returned {"status": "ok", "count": 1}]', handed_over[-1].parts[0].text)

    async def test_both_quotas_exhausted_use_live_transcripts_with_audio_muted(self):
        async def commit(session):
            await session.call("test__count", {"step": "live"})

        async def result(session, response):
            await session.say("Live counted.")

        live = FakeGemini(on_commit=commit, on_result=result)
        agent = self.agent({FAST_MODEL: [quota()], BRAIN_MODEL: [quota()]}, live)
        updates = []
        text, model = await agent.reply(self.request(history=(
            {"role": "user", "text": "Earlier", "files": ()},)), lambda t, m: updates.append((t, m)))
        self.assertEqual((text, model), ("Live counted.", LIVE_MODEL))
        self.assertEqual(updates[-1], ("Live counted.", LIVE_MODEL))
        self.assertEqual(self.tool_calls, [{"step": "live"}])
        session = live.sessions[0]
        self.assertIsNotNone(session.config.output_audio_transcription)
        self.assertEqual(session.functions["test__count"].behavior, types.Behavior.BLOCKING)
        self.assertEqual([c.role for c in session.seeds[0]], ["user"])
        self.assertIn("Earlier", session.seeds[0][0].parts[0].text)
        # A second request skips both cooling models instead of retrying them.
        live.on_commit = lambda s: s.say("Again.")
        self.assertEqual(await agent.reply(self.request(), lambda *a: None), ("Again.", LIVE_MODEL))
        self.assertEqual(len(self.client.models.calls), 2)

    def test_step_labels_hide_plumbing_and_use_server_titles_never_hashes(self):
        titles = SimpleNamespace(tool_title=lambda name: {
            "drive__3f2a9c0b7d1e4a6b8c9d0e1f": ("drive", "3f2a9c0b7d1e4a6b8c9d0e1f"),
            "calendar__events_insert": ("calendar", "Create calendar event"),
        }.get(name))
        self.assertIsNone(step_label("connect_integration", {"integration_id": "spotify"}, {}))
        self.assertIsNone(step_label("read_tool_result", {"result_id": "x"}, {}))
        self.assertEqual(step_label("calendar__events_insert", {}, {}, titles), ("Calendar", "Create calendar event"))
        self.assertEqual(step_label("drive__3f2a9c0b7d1e4a6b8c9d0e1f", {}, {}, titles), ("Drive", "Used Drive"))
        self.assertEqual(step_label("spotify__playMusic", {}, {"status": "failed"}), ("Spotify", "Play music (failed)"))

    async def test_serve_reports_failures_in_plain_language_and_interrupts_pending_replies_on_stop(self):
        class ClosedLive:
            def connect(self, **kwargs):
                raise quota()

        agent = self.agent({FAST_MODEL: [quota(), RuntimeError("boom")], BRAIN_MODEL: [quota()]}, ClosedLive())
        requests, events = queue.Queue(), []
        serving = asyncio.create_task(agent.serve(requests, events.append))
        requests.put(self.request())
        for _ in range(200):
            if events:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(events[-1].status, "failed")
        self.assertEqual(events[-1].error, "Luna has reached its usage limit for now. Try again in a few minutes.")
        for word in ("Gemini", "Flash", "Live", "quota"):
            self.assertNotIn(word, events[-1].error)
        requests.put(None)
        await asyncio.wait_for(serving, 2)


if __name__ == "__main__":
    unittest.main()
