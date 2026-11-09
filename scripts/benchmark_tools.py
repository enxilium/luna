"""Benchmark Gemini Live with full recorded MCP schemas and synthetic service effects.

uv run python -m scripts.benchmark_tools --audible --repeats 3
The microphone and real service effects are disabled.
"""

import argparse
import asyncio
import hashlib
import json
import re
import statistics
import time
from pathlib import Path

import httpx
from aiohttp import web
from loguru import logger
from mcp.types import Tool
from pipecat.frames.frames import (
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InputTextRawFrame,
    LLMTextFrame,
    MetricsFrame,
    TTSAudioRawFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from scripts.fixtures.workspace_api import APIFixture, FakeAuth
from src.agent.services import MODEL, build_llm_service
from src.audio.apple import AppleAudioTransport
from src.integrations.mcp import MCPIntegration, MCPRuntime
from src.integrations.spotify import playback_uri
from src.integrations.workspace.client import GoogleAPI
from src.integrations.workspace.server import build_server

CASES = {
    "greeting": "Say hello in one short sentence.",
    "gmail": "List the names of my Gmail labels.",
    "calendar": "Show the next five events on my primary Google calendar between 2026-10-05T00:00:00-04:00 and 2026-10-06T00:00:00-04:00. Give their titles.",
    "docs": "Replace all Project Alpha with Project Luna in the Google document with documentId=doc-example, matching case. The document has already been read. Apply and verify the edit.",
    "discovery": "Show the next five items on my agenda between 2026-10-05T00:00:00-04:00 and 2026-10-06T00:00:00-04:00. Give their titles.",
    "spotify": "Check what is currently playing on Spotify.",
    "playback": "Play Bohemian Rhapsody by Queen on Spotify. Use the original studio recording.",
    "history_playback": "Play my favorite song on Spotify. Use the original studio recording.",
    "playlist": 'Play my Spotify playlist named "the hermit". Find it in my playlists first and use its identifier.',
}
CASES["multi_service"] = (
    CASES["calendar"] + " Also check what is currently playing on Spotify."
)
CASES["unsupported"] = "Delete my Google calendar, including the calendar itself."
CASES["large_result"] = (
    "Read the Google document with documentId=doc-large and tell me the validation code at the very end. Retrieve stored response pages as needed."
)
TRACK_ID = "4u7EnebtmKWzUH433cf5Qv"
TRACK_URI = f"spotify:track:{TRACK_ID}"
PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"
PLAYLIST_URI = f"spotify:playlist:{PLAYLIST_ID}"
CASES["explicit_playback"] = (
    f"Play this Spotify track: https://open.spotify.com/track/{TRACK_ID}"
)
FIXTURES = Path(__file__).parent / "fixtures"


class FixtureServer:
    def __init__(self, catalog):
        self.catalog = catalog
        self.calls = []
        self.replaced = False
        self.playing = None

    async def handle(self, request):
        if request.method != "POST":
            return web.Response(status=204 if request.method == "DELETE" else 405)
        body = await request.json()
        if "id" not in body:
            return web.Response(status=202)
        integration = request.match_info["integration"]
        method = body["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "luna-benchmark", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    t.model_dump(mode="json", by_alias=True, exclude={"output_schema"})
                    for t in self.catalog[integration][1]
                ]
            }
        elif method == "tools/call":
            name, args = body["params"]["name"], body["params"].get("arguments", {})
            call = {
                "integration": integration,
                "name": name,
                "arguments": args,
                "at": time.perf_counter(),
            }
            self.calls.append(call)
            value, error = {}, False
            if name == "list_events":
                value = {
                    "events": [
                        {
                            "title": f"Design review {i}",
                            "startTime": f"2026-10-05T{9 + i:02}:00:00-04:00",
                        }
                        for i in range(5)
                    ]
                }
            elif name in {"read_doc", "read_file_content"}:
                document_id = args.get("documentId", args.get("fileId"))
                value = (
                    {
                        "documentId": "doc-large",
                        "text": "Reference notes. " * 700
                        + "Validation code: LUNA-7429",
                    }
                    if document_id == "doc-large"
                    else {
                        "documentId": "doc-example",
                        "text": "Project Luna" if self.replaced else "Project Alpha",
                    }
                )
            elif name == "update_doc":
                requests = args.get("requests", [])
                self.replaced = bool(requests) and all(
                    r.get("replaceAllText", {}).get("containsText", {}).get("text")
                    == "Project Alpha"
                    and r["replaceAllText"].get("replaceText") == "Project Luna"
                    and r["replaceAllText"]["containsText"].get("matchCase") is True
                    for r in requests
                )
                value, error = {"updated": self.replaced}, not self.replaced
            elif name == "list_labels":
                value = {
                    "labels": [
                        {"labelId": "INBOX", "name": "INBOX", "labelType": "SYSTEM"}
                    ]
                }
            elif name == "getNowPlaying":
                value = "Nothing is currently playing on Spotify"
            elif name == "searchSpotify":
                rows = [
                    f'1. "Bohemian Rhapsody" by Queen (5:55) - ID: {TRACK_ID}',
                    '2. "Bohemian Rhapsody (Live)" by Queen (6:00) - ID: 6u7EnebtmKWzUH433cf5Qv',
                    '3. "Bohemian Rhapsody" by A Cover Band (5:55) - ID: 8u7EnebtmKWzUH433cf5Qv',
                ][: int(args.get("limit", 10))]
                value = (
                    f'# Search results for "{args.get("query")}" (type: track)\n\n'
                    + "\n".join(rows)
                )
            elif name == "getMyPlaylists":
                value = f'# Your Spotify Playlists (1-1 of 1)\n\n1. "the hermit" (12 tracks) - ID: {PLAYLIST_ID}'
            elif name == "getAvailableDevices":
                value = "# Available Spotify Devices\n\n1. Test Speaker (Speaker)\n   Status: ▶ Active | Volume: 50% | ID: device1"
            elif name == "playMusic":
                self.playing = (
                    args.get("uri", args.get("context_uri"))
                    or f"spotify:{args.get('type')}:{args.get('id')}"
                )
                error = self.playing not in {TRACK_URI, PLAYLIST_URI}
                value = (
                    "Error playing music: Incorrect synthetic identifier"
                    if error
                    else f"Now playing: {self.playing}"
                )
            else:
                value, error = {"error": "Not part of this synthetic task"}, True
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": value if isinstance(value, str) else json.dumps(value),
                    }
                ],
                "isError": error,
            }
            call["completed_at"] = time.perf_counter()
        else:
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32601, "message": "Unknown method"},
                }
            )
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})


def arguments_correct(case, calls, replaced):
    if case == "playlist":
        reads = [
            i
            for i, c in enumerate(calls)
            if c["integration"] == "spotify" and c["name"] == "getMyPlaylists"
        ]
        plays = [
            i
            for i, c in enumerate(calls)
            if c["integration"] == "spotify" and c["name"] == "playMusic"
        ]
        return bool(
            reads
            and len(plays) == 1
            and reads[0] < plays[0]
            and playback_uri(calls[plays[0]]["arguments"]) == PLAYLIST_URI
        )
    if case == "gmail":
        return (
            len(calls) == 1
            and calls[0]["integration"] == "google_gmail"
            and calls[0]["name"] == "list_labels"
        )
    if case == "multi_service":
        return arguments_correct("calendar", calls, replaced) and arguments_correct(
            "spotify", calls, replaced
        )
    if case == "unsupported":
        return not calls
    if case == "large_result":
        return any(
            c["name"] in {"read_doc", "read_file_content"}
            and c["arguments"].get("documentId", c["arguments"].get("fileId"))
            == "doc-large"
            for c in calls
        )
    if case == "greeting":
        return not calls
    if case in ("calendar", "discovery"):
        return any(
            c["integration"] == "google_calendar"
            and c["name"] == "list_events"
            and c["arguments"].get("calendarId", "primary") == "primary"
            and c["arguments"].get("orderBy") == "startTime"
            and c["arguments"].get("pageSize") == 5
            and c["arguments"].get("startTime") == "2026-10-05T00:00:00-04:00"
            and c["arguments"].get("endTime") == "2026-10-06T00:00:00-04:00"
            for c in calls
        )
    if case == "spotify":
        return any(
            c["integration"] == "spotify" and c["name"] == "getNowPlaying"
            for c in calls
        )
    if case in {"playback", "history_playback", "explicit_playback"}:
        searches = [
            i
            for i, c in enumerate(calls)
            if c["integration"] == "spotify"
            and c["name"] == "searchSpotify"
            and c["arguments"].get("type") == "track"
            and "bohemian" in c["arguments"].get("query", "").lower()
            and "queen" in c["arguments"].get("query", "").lower()
        ]
        plays = [i for i, c in enumerate(calls) if c["name"] == "playMusic"]
        if len(plays) != 1 or (
            case != "explicit_playback" and (not searches or searches[0] >= plays[0])
        ):
            return False
        args = calls[plays[0]]["arguments"]
        return args.get("uri", args.get("context_uri")) == TRACK_URI or (
            args.get("type") == "track" and args.get("id") == TRACK_ID
        )
    edits = [i for i, c in enumerate(calls) if c["name"] == "update_doc"]
    return (
        replaced
        and len(edits) == 1
        and calls[edits[0]]["arguments"].get("documentId") == "doc-example"
        and any(
            c["name"] in {"read_doc", "read_file_content"}
            and c["arguments"].get("documentId", c["arguments"].get("fileId"))
            == "doc-example"
            for c in calls[edits[0] + 1 :]
        )
    )


def answer_correct(case, text):
    text = text.lower().replace("’", "'")
    if case == "gmail":
        return "inbox" in text
    if case in {"calendar", "discovery", "multi_service"}:
        return all(f"design review {i}" in text for i in range(5))
    if case == "large_result":
        return "LUNA7429" in re.sub(r"[^a-zA-Z0-9]", "", text).upper()
    if case == "unsupported":
        return any(
            word in text for word in ("can't", "cannot", "unable", "not", "don't")
        )
    return True


async def run_case(runtime, prompt, *, audible=False, timeout=90, history=None):
    llm = build_llm_service(runtime)
    context = llm.context
    for message in history or []:
        context.add_message(message)
    user, assistant = LLMContextAggregatorPair(context)
    done = asyncio.Event()
    audio_grace = None
    spoken = []
    started = None
    metrics = {
        "first_audio_seconds": None,
        "first_speaker_delivery_seconds": None,
        "first_speaker_render_seconds": None,
        "final_tool_result_seconds": None,
        "reply_complete_seconds": None,
        "token_usage": [],
        "errors": [],
        "native_tool_calls": [],
        "tool_results": [],
        "model_parts": [],
    }
    configs = []
    connect = llm._client.aio.live.connect

    def capture_config(**kwargs):
        configs.append(kwargs["config"].model_dump(mode="json", exclude_none=True))
        return connect(**kwargs)

    llm._client.aio.live.connect = capture_config
    model_turn = llm._handle_msg_model_turn

    async def record_model_parts(message):
        for part in message.server_content.model_turn.parts or []:
            metrics["model_parts"].append(
                {
                    "text": bool(part.text),
                    "mime_type": part.inline_data.mime_type
                    if part.inline_data
                    else None,
                    "audio_bytes": len(part.inline_data.data or b"")
                    if part.inline_data
                    else 0,
                }
            )
        await model_turn(message)

    llm._handle_msg_model_turn = record_model_parts

    class Probe(FrameProcessor):
        async def process_frame(self, frame, direction):
            nonlocal audio_grace
            await super().process_frame(frame, direction)
            if started is not None and direction == FrameDirection.DOWNSTREAM:
                elapsed = time.perf_counter() - started
                if (
                    isinstance(frame, TTSAudioRawFrame)
                    and frame.audio
                    and metrics["first_audio_seconds"] is None
                ):
                    metrics["first_audio_seconds"] = elapsed
                    if audio_grace:
                        audio_grace.cancel()
                elif isinstance(frame, LLMTextFrame):
                    spoken.append(frame.text)
                elif isinstance(frame, FunctionCallResultFrame):
                    metrics["final_tool_result_seconds"] = elapsed
                    result = frame.result if isinstance(frame.result, dict) else {}
                    metrics["tool_results"].append(
                        {
                            "tool": frame.function_name,
                            **{
                                k: result[k]
                                for k in ("status", "reason", "path", "constraint")
                                if k in result
                            },
                        }
                    )
                elif isinstance(frame, FunctionCallsStartedFrame):
                    metrics["native_tool_calls"].extend(
                        {"tool": c.function_name, "arguments": c.arguments}
                        for c in frame.function_calls
                    )
                elif isinstance(frame, MetricsFrame):
                    metrics["token_usage"].extend(
                        x.value.model_dump()
                        for x in frame.data
                        if isinstance(x, LLMUsageMetricsData)
                    )
            await self.push_frame(frame, direction)

    processors = [user, llm, Probe()]
    if audible:
        output = AppleAudioTransport(audio_in_enabled=False).output()

        @output.event_handler("on_audio_delivered")
        async def delivered(transport, frame):
            if (
                started is not None
                and metrics["first_speaker_delivery_seconds"] is None
            ):
                metrics["first_speaker_delivery_seconds"] = (
                    time.perf_counter() - started
                )

        @output.event_handler("on_audio_rendered")
        async def rendered(transport, frame):
            if started is not None and metrics["first_speaker_render_seconds"] is None:
                metrics["first_speaker_render_seconds"] = time.perf_counter() - started

        processors.append(output)
    processors.append(assistant)
    worker = PipelineWorker(
        Pipeline(processors),
        params=PipelineParams(
            enable_metrics=True, enable_usage_metrics=True, audio_out_sample_rate=24000
        ),
        enable_rtvi=False,
        idle_timeout_secs=None,
        app_resources=runtime,
    )

    @worker.event_handler("on_pipeline_error")
    async def on_error(worker, frame):
        metrics["errors"].append("pipeline_error")
        done.set()

    @assistant.event_handler("on_assistant_turn_stopped")
    async def on_reply(aggregator, message):
        nonlocal audio_grace
        if started is not None:
            metrics["reply_complete_seconds"] = time.perf_counter() - started
            if metrics["first_audio_seconds"] is not None:
                done.set()
            elif audio_grace is None:
                # A text-only turn boundary can precede audio; keep receiving instead of cutting it off.
                async def wait_for_audio():
                    await asyncio.sleep(5)
                    if metrics["first_audio_seconds"] is None:
                        metrics["errors"].append("no_generated_audio")
                        metrics["reply_complete_seconds"] = None
                        done.set()

                audio_grace = asyncio.create_task(wait_for_audio())

    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    before_ready = time.perf_counter()
    running = asyncio.create_task(runner.run())
    try:
        await llm.wait_until_ready()
        metrics["ready_seconds"] = time.perf_counter() - before_ready
        started = time.perf_counter()
        await worker.queue_frame(InputTextRawFrame(prompt))
        await asyncio.wait_for(done.wait(), timeout)
    except (TimeoutError, RuntimeError):
        metrics["errors"].append("timeout_or_connection_failure")
    finally:
        if audio_grace:
            audio_grace.cancel()
            await asyncio.gather(audio_grace, return_exceptions=True)
        metrics["spoken_text"] = "".join(spoken)
        metrics["started_at"] = started
        metrics["setup_schema_bytes"] = (
            len(json.dumps(configs[-1].get("tools", [])).encode()) if configs else None
        )
        metrics["setup_tool_count"] = (
            sum(
                len(t.get("function_declarations", []))
                for t in configs[-1].get("tools", [])
            )
            if configs
            else 0
        )
        metrics["latency_miss"] = (
            metrics["first_speaker_render_seconds"] is None
            or metrics["first_speaker_render_seconds"] > 2
        )
        await worker.cancel()
        await asyncio.wait_for(running, 10)
    return metrics


async def main(args):
    logger.remove()
    logger.add(lambda message: None, level="WARNING")
    snapshot_bytes = Path(args.snapshot).read_bytes()
    snapshot = json.loads(snapshot_bytes)
    catalog = {
        i: (
            r["description"],
            [
                Tool.model_validate(t)
                for t in r["tools"]
                if r.get("allowlist") is None or t["name"] in r["allowlist"]
            ],
        )
        for i, r in snapshot.items()
    }
    server = FixtureServer(catalog)
    api_fixture = APIFixture()

    async def fake_api_request(request):
        await asyncio.sleep(0.005)
        return api_fixture(request)

    api_http = httpx.AsyncClient(transport=httpx.MockTransport(fake_api_request))

    def local_integration(integration_id, description):
        service = integration_id.removeprefix("google_")
        backend = build_server(service, GoogleAPI(FakeAuth(service), api_http))

        async def record(context, next_handler):
            if context.method != "tools/call":
                return await next_handler(context)
            params = context.params or {}
            call = {
                "integration": integration_id,
                "name": params.get("name"),
                "arguments": params.get("arguments", {}),
                "at": time.perf_counter(),
            }
            server.calls.append(call)
            try:
                return await next_handler(context)
            finally:
                call["completed_at"] = time.perf_counter()

        backend.middleware.append(record)
        return MCPIntegration(integration_id, description, backend)

    app = web.Application()
    app.router.add_route("*", "/mcp/{integration}", server.handle)
    http_runner = web.AppRunner(app, access_log=None)
    await http_runner.setup()
    site = web.TCPSite(http_runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    rows = []
    try:
        for repeat in range(args.repeats):
            for case in args.cases.split(","):
                print(f"Running {case} ({repeat + 1}/{args.repeats})", flush=True)
                server.calls.clear()
                server.replaced = False
                server.playing = None
                api_fixture.replaced = False
                api_fixture.calls.clear()
                async with MCPRuntime(
                    [
                        local_integration(i, desc)
                        if args.backend == "local" and i.startswith("google_")
                        else MCPIntegration(i, desc, f"{base}/mcp/{i}")
                        for i, (desc, _) in catalog.items()
                    ]
                ) as runtime:
                    history = (
                        [
                            {
                                "role": "user",
                                "content": "My favorite song is Bohemian Rhapsody by Queen, the original studio recording.",
                            },
                            {"role": "assistant", "content": "Understood."},
                        ]
                        if case == "history_playback"
                        else None
                    )
                    row = await run_case(
                        runtime,
                        CASES[case],
                        audible=args.audible,
                        timeout=args.timeout,
                        history=history,
                    )
                started = row.pop("started_at")
                if args.backend == "local":
                    server.replaced = api_fixture.replaced
                allowed = {
                    "greeting": set(),
                    "gmail": {"list_labels"},
                    "calendar": {"list_events"},
                    "discovery": {"list_events"},
                    "docs": {"update_doc", "read_doc", "read_file_content"},
                    "spotify": {"getNowPlaying"},
                    "playback": {"searchSpotify", "playMusic"},
                    "history_playback": {"searchSpotify", "playMusic"},
                    "playlist": {"getMyPlaylists", "getAvailableDevices", "playMusic"},
                    "explicit_playback": {"playMusic", "searchSpotify"},
                    "large_result": {"read_doc", "read_file_content"},
                    "multi_service": {"list_events", "getNowPlaying"},
                    "unsupported": set(),
                }[case]
                correct = arguments_correct(
                    case, server.calls, server.replaced
                ) and all(c["name"] in allowed for c in server.calls)
                row.update(
                    case=case,
                    backend=args.backend,
                    prompt=CASES[case],
                    repeat=repeat + 1,
                    model=MODEL,
                    snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
                    task_correct=bool(
                        correct
                        and answer_correct(case, row["spoken_text"])
                        and row["first_audio_seconds"] is not None
                        and not row["errors"]
                    ),
                    service_calls=[
                        {k: v for k, v in c.items() if k not in {"at", "completed_at"}}
                        for c in server.calls
                    ],
                    service_complete_seconds=next(
                        (
                            c["completed_at"] - started
                            for index, c in enumerate(server.calls)
                            if started is not None
                            and arguments_correct(
                                case, server.calls[: index + 1], server.replaced
                            )
                        ),
                        None,
                    ),
                )
                rows.append(row)
                if args.backend == "local":
                    row["api_requests"] = [
                        {k: v for k, v in c.items() if k != "headers"}
                        for c in api_fixture.calls
                    ]
                Path(args.output).write_text(json.dumps(rows, indent=2))
                print(json.dumps(row), flush=True)
    finally:
        await http_runner.cleanup()
        await api_http.aclose()
    summary = {
        "turns": len(rows),
        "correct": sum(r["task_correct"] for r in rows),
        "latency_misses": sum(r["latency_miss"] for r in rows),
        "cases": {
            c: {
                "turns": sum(r["case"] == c for r in rows),
                "first_audio_median_seconds": statistics.median(
                    r["first_audio_seconds"]
                    for r in rows
                    if r["case"] == c and r["first_audio_seconds"] is not None
                )
                if any(
                    r["case"] == c and r["first_audio_seconds"] is not None
                    for r in rows
                )
                else None,
            }
            for c in args.cases.split(",")
        },
    }
    Path(args.output).with_suffix(".summary.json").write_text(
        json.dumps(summary, indent=2)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="/tmp/luna-gemini-tools.json")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--cases", default="greeting,calendar,docs,playback,history_playback,spotify"
    )
    parser.add_argument("--snapshot", default=str(FIXTURES / "mcp-catalog.json"))
    parser.add_argument("--audible", action="store_true")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument(
        "--backend",
        choices=("recorded", "local"),
        default="recorded",
        help="Benchmark-only choice: recorded MCP effects or the local backend over synthetic REST effects",
    )
    asyncio.run(main(parser.parse_args()))
