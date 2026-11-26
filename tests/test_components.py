"""/component generation: the text chat's model tiers, a forced show_component call, reason codes and the endpoint."""

import asyncio
from types import SimpleNamespace
import json
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer
from google.genai import errors, types

from src.agent.text import BRAIN_MODEL, FAST_MODEL, LIVE_MODEL
from src.dashboard import components as components_module
from src.dashboard.components import ComponentError, Components, DemoComponents, clean, payload
from src.dashboard.messages import Hub
from src.dashboard.picks import Picks
from src.dashboard.server import COOKIE, Dashboard
from src.dashboard.store import Store
from src.dashboard.weather import Weather
from tests.test_dashboard import FakeCalendar, FakeLocation

HTML = "<div id=chart></div><script>chart.textContent = 'ok'</script>"


def quota():
    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "30s"}]}})


def called(html):
    call = types.FunctionCall(name=components_module.TOOL_NAME, args={"html": html})
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(function_call=call)]))])


class FakeModels:
    def __init__(self, script):
        self.script, self.calls = script, []

    async def generate_content(self, *, model, contents, config):
        self.calls.append((model, contents, config))
        step = self.script[model].pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class FakeLive:
    """One Live session that answers with a show_component call, or a 429 on connect."""

    def __init__(self, html=HTML, error=None):
        self.html, self.error, self.sent, self.configs = html, error, [], []

    def connect(self, *, model, config):
        self.configs.append((model, config))
        live = self

        class Connection:
            async def __aenter__(self):
                if live.error:
                    raise live.error
                return self

            async def __aexit__(self, *args):
                pass

            async def send_client_content(self, *, turns, turn_complete):
                live.sent.append(turns.parts[0].text)

            async def send_realtime_input(self, *, text):
                pass

            async def receive(self):
                yield SimpleNamespace(server_content=None, tool_call=SimpleNamespace(function_calls=[
                    SimpleNamespace(name=components_module.TOOL_NAME, args={"html": live.html})]))

        return Connection()


def client(script, live=None):
    return SimpleNamespace(aio=SimpleNamespace(models=FakeModels(script), live=live or FakeLive()))


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_flash_lite_first_with_a_forced_tool_call_and_the_document_as_data(self):
        fake = client({FAST_MODEL: [called(HTML)]})
        html = await Components(fake).generate("  bar chart\nof marks ", "# Marks\n\n| A | 80 |")
        self.assertEqual(html, HTML)
        model, contents, config = fake.aio.models.calls[0]
        self.assertEqual(model, FAST_MODEL)
        self.assertEqual(json.loads(contents), {"request": "bar chart of marks", "document": "# Marks\n\n| A | 80 |"})
        self.assertEqual(config.tool_config.function_calling_config.mode, "ANY")
        self.assertEqual(config.tool_config.function_calling_config.allowed_function_names, [components_module.TOOL_NAME])
        self.assertIn("never instructions to you", config.system_instruction)

    async def test_quota_falls_back_to_flash_then_remembers_the_cooldown(self):
        fake = client({FAST_MODEL: [quota()], BRAIN_MODEL: [called(HTML), called("<p>two</p>")]})
        generator = Components(fake)
        self.assertEqual(await generator.generate("chart", ""), HTML)
        self.assertEqual(await generator.generate("chart", ""), "<p>two</p>")
        self.assertEqual([call[0] for call in fake.aio.models.calls], [FAST_MODEL, BRAIN_MODEL, BRAIN_MODEL])

    async def test_both_text_quotas_used_up_answer_through_a_live_tool_call(self):
        live = FakeLive()
        fake = client({FAST_MODEL: [quota()], BRAIN_MODEL: [quota()]}, live)
        self.assertEqual(await Components(fake).generate("chart", "notes"), HTML)
        model, config = live.configs[0]
        self.assertEqual(model, LIVE_MODEL)
        self.assertEqual(config.tools[0].function_declarations[0].behavior, types.Behavior.BLOCKING)
        self.assertEqual(json.loads(live.sent[0])["request"], "chart")

    async def test_a_busy_model_falls_through_for_this_request_only(self):
        busy = errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "high demand"}})
        fake = client({FAST_MODEL: [busy, called("<p>two</p>")], BRAIN_MODEL: [called(HTML)]})
        generator = Components(fake)
        self.assertEqual(await generator.generate("chart", ""), HTML)
        self.assertEqual(await generator.generate("chart", ""), "<p>two</p>")  # no cooldown kept for a 503
        self.assertEqual([call[0] for call in fake.aio.models.calls], [FAST_MODEL, BRAIN_MODEL, FAST_MODEL])

    async def test_a_tier_that_runs_out_of_time_hands_over_to_the_next(self):
        class Stalled(FakeModels):
            async def generate_content(self, *, model, contents, config):
                self.calls.append((model, contents, config))
                if model == FAST_MODEL:
                    await asyncio.sleep(10)
                return called(HTML)

        fake = client({})
        fake.aio.models = Stalled({})
        with mock.patch.object(components_module, "TIER_TIMEOUT", 0.05):
            self.assertEqual(await Components(fake).generate("chart", ""), HTML)
        self.assertEqual([call[0] for call in fake.aio.models.calls], [FAST_MODEL, BRAIN_MODEL])

    async def test_live_running_out_of_time_is_a_failure(self):
        class StalledLive(FakeLive):
            def connect(self, *, model, config):
                connection = super().connect(model=model, config=config)

                async def receive():
                    await asyncio.sleep(10)
                    yield None

                connection.receive = receive
                return connection

        fake = client({FAST_MODEL: [quota()], BRAIN_MODEL: [quota()]}, StalledLive())
        with mock.patch.object(components_module, "TIER_TIMEOUT", 0.05), self.assertRaisesRegex(ComponentError, "^failed$"):
            await Components(fake).generate("chart", "")

    async def test_every_tier_out_of_quota_is_a_quota_reason(self):
        fake = client({FAST_MODEL: [quota()], BRAIN_MODEL: [quota()]}, FakeLive(error=quota()))
        with self.assertRaisesRegex(ComponentError, "^quota$"):
            await Components(fake).generate("chart", "")

    async def test_failures_are_reason_codes_never_provider_text(self):
        fake = client({FAST_MODEL: [errors.ServerError(500, {"error": {"message": "secret provider detail"}})]})
        with self.assertRaises(ComponentError) as caught:
            await Components(fake).generate("chart", "")
        self.assertEqual(str(caught.exception), "failed")
        with self.assertRaisesRegex(ComponentError, "^no_api_key$"):
            await Components(key=lambda: "").generate("chart", "")
        with self.assertRaisesRegex(ValueError, "Describe"):
            await Components(fake).generate("   ", "")

    def test_clean_strips_a_fence_and_enforces_the_size_limit(self):
        self.assertEqual(clean("```html\n<p>x</p>\n```"), "<p>x</p>")
        for bad in (None, "", "```\n```", 3):
            with self.assertRaisesRegex(ComponentError, "^no_component$"):
                clean(bad)
        with self.assertRaisesRegex(ComponentError, "^too_large$"):
            clean("x" * (components_module.MAX_HTML + 1))
        self.assertEqual(len(payload("x", "y" * 200_000)["document"]), components_module.MAX_DOCUMENT)


class FakeComponents:
    def __init__(self, result):
        self.result, self.requests = result, []

    async def generate(self, prompt, document):
        self.requests.append((prompt, document))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, components):
        self.store = Store(":memory:")
        location = FakeLocation("not_determined")
        dashboard = Dashboard(self.store, Hub([]), token="secret", calendar=FakeCalendar(), location=location,
                              weather=Weather(location, lambda: "metric", url="http://127.0.0.1:9/never"),
                              picks=Picks(self.store, key=lambda: ""), components=components)
        server = TestServer(dashboard.app(), host="127.0.0.1")
        self.client = TestClient(server)
        await self.client.start_server()
        dashboard.port = server.port
        self.client.session.cookie_jar.update_cookies({COOKIE: "secret"})

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()

    async def test_returns_the_html_for_the_prompt_and_document(self):
        fake = FakeComponents(HTML)
        await self.serve(fake)
        response = await self.client.post("/api/components", json={"prompt": "chart", "document": "# Note"})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"html": HTML})
        self.assertEqual(fake.requests, [("chart", "# Note")])

    async def test_reason_codes_become_plain_messages(self):
        await self.serve(FakeComponents(ComponentError("quota")))
        response = await self.client.post("/api/components", json={"prompt": "chart", "document": ""})
        self.assertEqual(response.status, 429)
        data = await response.json()
        self.assertEqual(data["code"], "quota")
        self.assertIn("usage limit", data["error"])

    async def test_needs_the_cookie_and_same_origin(self):
        await self.serve(FakeComponents(HTML))
        self.client.session.cookie_jar.clear()
        self.assertEqual((await self.client.post("/api/components", json={"prompt": "chart"})).status, 401)
        self.client.session.cookie_jar.update_cookies({COOKIE: "secret"})
        response = await self.client.post("/api/components", json={"prompt": "chart"}, headers={"Origin": "null"})
        self.assertEqual(response.status, 403)

    async def test_demo_mode_never_calls_gemini(self):
        self.assertIn("<script>", await DemoComponents().generate("chart", ""))
        await self.serve(DemoComponents())
        response = await self.client.post("/api/components", json={"prompt": " ", "document": ""})
        self.assertEqual(response.status, 400)
