"""Typed chat replies: Gemini Flash models with MCP tools, falling back to Live transcripts when quotas run out."""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from uuid import UUID, uuid4

from google import genai
from google.genai import errors, types
from loguru import logger
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams

from src.agent.services import MODEL as LIVE_MODEL, SYSTEM_INSTRUCTION, api_key, gemini_tools
from src.agent.weather import tools as weather_tools


# Google has no 3.8 Flash-Lite text model (only gemini-3.8-flash-lite-tts); 3.5 is the newest GA Flash-Lite.
FAST_MODEL = "gemini-3.5-flash-lite"
BRAIN_MODEL = "gemini-3.8-flash"
TEXT_INSTRUCTION = (
    " This conversation is typed in Luna's text chat window, not spoken. Replies appear as plain text: "
    "use short paragraphs or simple dashes, never markdown syntax."
)
QUOTA_COOLDOWN = 60.0
MAX_TOOL_ROUNDS = 16
TOOL_TIMEOUT = 300.0
LIVE_CONNECT_TIMEOUT = 10.0
LIVE_REPLY_TIMEOUT = 360.0
LIVE_SEED_BYTES = 32000


@dataclass(frozen=True)
class TextRequest:
    chat_id: str
    message_id: int
    history: tuple  # Earlier completed turns, oldest first: {"role": "user"|"luna", "text", "files"}.
    text: str
    files: tuple = ()
    brain: bool = False


@dataclass(frozen=True)
class TextEvent:
    chat_id: str
    message_id: int
    text: str
    status: str = "pending"  # pending | complete | failed | interrupted
    model: str = ""  # Stored for the log and history; the UI never shows it.
    error: str = ""
    steps: tuple = ()  # (service, label) per tool call so far, for the chat's step list.


class QuotaExhausted(Exception):
    def __init__(self, retry_after=QUOTA_COOLDOWN):
        super().__init__("quota_exhausted")
        self.retry_after = retry_after


def with_files(text, files):
    if not files:
        return text
    names = ", ".join(Path(path).name for path in files)
    note = f"[The user attached local files: {names}. Their contents were not shared with you.]"
    return f"{text}\n\n{note}" if text else note


def humanize(name):
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ").replace("-", " ").split()
    return " ".join(words).capitalize() if words else "Tool"


# Mechanics the user never asked for: reconnecting a service, paging through a long result.
PLUMBING = {"connect_integration", "read_tool_result"}


def step_label(name, arguments, result, integrations=None):
    """A short, user-facing (service, label) for one tool call, or None for plumbing.

    It never includes arguments or results, and prefers the title the tool's MCP server gives it.
    """
    if name in PLUMBING:
        return None
    integration = arguments.get("integration_id") if isinstance(arguments, dict) else None
    service = humanize(integration).title() if isinstance(integration, str) else ""
    if name == "recall_memory":
        service, label = "Memory", "Searched memory"
    elif name == "remember_memory":
        service, label = "Memory", "Saved to memory"
    elif name == "get_weather":
        service, label = "Weather", "Checked the weather"
    elif name == "authorize_integration":
        label = f"Signed in to {service or 'a service'}"
    elif "__" in name:
        origin = integrations.tool_title(name) if hasattr(integrations, "tool_title") else None
        integration, tool = origin or name.split("__", 1)
        service = humanize(integration).title()
        if re.fullmatch(r"[0-9a-f]{24}", tool):
            label = f"Used {service}"  # A hashed published name: never show it.
        else:
            label = tool if " " in tool else humanize(tool)
    else:
        label = humanize(name)
    if isinstance(result, dict) and result.get("status") == "failed":
        label += " (failed)"
    return service, label


def failure_message(error):
    """What the user sees: plain language, no model names. The technical reason stays in the log."""
    if isinstance(error, QuotaExhausted):
        return "Luna has reached its usage limit for now. Try again in a few minutes."
    return "Luna couldn't finish that reply. Check your internet connection and try again."


def retry_after(error):
    match = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', json.dumps(error.details, default=str))
    return min(float(match.group(1)), 3600.0) if match else QUOTA_COOLDOWN


def merge(contents):
    """Join consecutive same-role turns; a failed reply can leave two user turns in a row."""
    merged = []
    for content in contents:
        if merged and merged[-1].role == content.role:
            merged[-1] = types.Content(role=content.role, parts=[*merged[-1].parts, *content.parts])
        else:
            merged.append(content)
    return merged


def flatten(contents):
    """Render tool exchanges as text, for a model that did not issue these calls (signatures are model-specific)."""
    flat = []
    for content in contents:
        lines = []
        for part in content.parts or []:
            if part.function_call:
                call = part.function_call
                lines.append(f"[Called {call.name} with {json.dumps(call.args or {}, ensure_ascii=False)}]")
            elif part.function_response:
                response = part.function_response
                lines.append(f"[Function {response.name} returned {json.dumps(response.response, ensure_ascii=False, default=str)}]")
            elif part.text and not part.thought:
                lines.append(part.text)
        if lines:
            flat.append(types.Content(role=content.role, parts=[types.Part(text="\n".join(lines))]))
    return merge(flat)


def bounded(contents, limit=LIVE_SEED_BYTES):
    """Keep recent whole user turns within Live's seed budget, as the voice path does."""
    turns = [[]]
    for content in contents:
        if content.role == "user" and turns[-1]:
            turns.append([])
        turns[-1].append(content)
    kept, size = [], 0
    for turn in reversed(turns):
        length = sum(len(content.model_dump_json(exclude_none=True).encode()) for content in turn)
        if kept and size + length > limit:
            logger.warning("Live text fallback will use recent chat history only.")
            break
        kept[:0] = turn
        size += length
    return kept


class TextAgent:
    def __init__(self, integrations, memory=None, *, client=None):
        self.integrations, self.memory = integrations, memory
        self.client = client or genai.Client(api_key=api_key())
        self._exhausted = {}
        self._notes = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aio.aclose()
        self.client.close()

    def models(self, brain):
        order = (BRAIN_MODEL, FAST_MODEL) if brain else (FAST_MODEL, BRAIN_MODEL)
        now = time.monotonic()
        return [model for model in order if self._exhausted.get(model, 0) <= now]

    async def serve(self, requests, publish):
        """Answer requests from a thread queue until it yields None; each chat streams independently."""
        tasks = set()
        try:
            while (request := await asyncio.to_thread(requests.get)) is not None:
                task = asyncio.create_task(self._answer(request, publish), name=f"luna-text-{request.message_id}")
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _answer(self, request, publish):
        shown = {"text": "", "model": "", "steps": ()}

        def event(status, error="", **values):
            shown.update(values)
            return TextEvent(request.chat_id, request.message_id, shown["text"], status, shown["model"], error,
                             shown["steps"])

        def emit(text, model):
            publish(event("pending", text=text, model=model))

        def step(item):
            if item is not None:
                publish(event("pending", steps=(*shown["steps"], item)))

        try:
            text, model = await self.reply(request, emit, step)
        except asyncio.CancelledError:
            publish(event("interrupted"))
            raise
        except QuotaExhausted as error:
            logger.warning("Every Gemini text route is out of quota.")
            publish(event("failed", failure_message(error)))
        except Exception as error:
            logger.warning("Text reply failed ({}).", type(error).__name__)
            publish(event("failed", failure_message(error)))
        else:
            publish(event("complete", text=text, model=model))

    async def _record(self, speaker, text, conversation_id):
        if self.memory:
            try:
                await self.memory.record(speaker, text, conversation_id=conversation_id)
            except Exception as error:
                logger.warning("Could not save text chat memory ({}).", type(error).__name__)

    async def _instruction(self, conversation_id):
        instruction = SYSTEM_INSTRUCTION + TEXT_INSTRUCTION
        if self.integrations.integrations:
            instruction += "\n\n" + self.integrations.system_instruction()
        if self.memory:
            # One snapshot per chat keeps the instruction prefix stable; recall_memory covers newer facts.
            if conversation_id not in self._notes:
                self._notes[conversation_id] = await self.memory.startup_context(conversation_id=conversation_id)
            instruction += "\n\n" + self._notes[conversation_id]
        return instruction

    def _tools(self, conversation_id):
        schema = self.integrations.tools_schema()
        schema.standard_tools.extend(weather_tools())
        if self.memory:
            schema.standard_tools.extend(self.memory.tools(conversation_id=conversation_id))
        return gemini_tools(schema), {tool.name: tool.handler for tool in schema.standard_tools}

    async def reply(self, request, on_text, on_step=None):
        """Return (text, model). Tool results stay in the turn across model switches, so no call is replayed."""
        on_step = on_step or (lambda item: None)
        conversation_id = UUID(request.chat_id)
        prompt = with_files(request.text, request.files)
        await self._record("user", prompt, conversation_id)
        instruction = await self._instruction(conversation_id)
        declarations, handlers = self._tools(conversation_id)
        turns = [*request.history, {"role": "user", "text": request.text, "files": request.files}]
        context = LLMContext(messages=[
            {"role": "user", "content": with_files(turn["text"], turn.get("files"))}
            for turn in turns if turn["role"] == "user"
        ])
        contents = merge([
            types.Content(role="user" if turn["role"] == "user" else "model",
                          parts=[types.Part(text=with_files(turn["text"], turn.get("files")) or " ")])
            for turn in turns
        ])
        start = len(contents) - 1
        blocks, used, rounds = [], None, 0

        def show(model, current=""):
            on_text("\n\n".join(block for block in [*blocks, current.strip()] if block), model)

        while True:
            models = self.models(request.brain)
            if not models:
                text = await self._live(contents, instruction, declarations, handlers, context,
                                        lambda current: show(LIVE_MODEL, current), on_step)
                blocks.append(text.strip())
                reply, used = "\n\n".join(block for block in blocks if block), LIVE_MODEL
                break
            model = models[0]
            if used and model != used:
                contents = contents[:start] + flatten(contents[start:])
            current = []

            def delta(text):
                current.append(text)
                show(model, "".join(current))

            try:
                parts = await self._generate(model, contents, instruction, declarations, delta)
            except QuotaExhausted as error:
                self._exhausted[model] = time.monotonic() + error.retry_after
                logger.warning("{} is out of quota; trying the next route.", model)
                show(used or model)
                continue
            used = model
            blocks.append("".join(current).strip())
            contents.append(types.Content(role="model", parts=parts))
            calls = [part.function_call for part in parts if part.function_call]
            if not calls:
                reply = "\n\n".join(block for block in blocks if block)
                break
            rounds += 1
            if rounds > MAX_TOOL_ROUNDS:
                raise RuntimeError("too_many_tool_rounds")
            responses = []
            for call in calls:
                result = await self._call(handlers, call, context)
                on_step(step_label(call.name, call.args, result, self.integrations))
                responses.append(types.Part(function_response=types.FunctionResponse(
                    id=call.id, name=call.name, response=result)))
            contents.append(types.Content(role="user", parts=responses))
        await self._record("assistant", reply, conversation_id)
        return reply, used

    async def _generate(self, model, contents, instruction, declarations, on_delta):
        config = types.GenerateContentConfig(system_instruction=instruction, tools=declarations or None)
        parts = []
        try:
            stream = await self.client.aio.models.generate_content_stream(model=model, contents=contents, config=config)
            async for chunk in stream:
                candidate = (chunk.candidates or [None])[0]
                for part in (candidate and candidate.content and candidate.content.parts) or []:
                    # Keep every part as sent: thought signatures must return with their function calls.
                    parts.append(part)
                    if part.text and not part.thought:
                        on_delta(part.text)
        except errors.APIError as error:
            if error.code == 429:
                raise QuotaExhausted(retry_after(error)) from None
            raise
        return parts

    async def _call(self, handlers, call, context):
        handler = handlers.get(call.name)
        if handler is None:
            return {"status": "failed", "reason": "unknown_tool"}
        results = []

        async def callback(result, *, properties=None):
            if not (properties and properties.is_final is False) and not results:
                results.append(result)

        params = FunctionCallParams(call.name, call.id or uuid4().hex, dict(call.args or {}),
                                    None, None, context, callback, app_resources=self.integrations)
        try:
            async with asyncio.timeout(getattr(handler, "_pipecat_timeout_secs", None) or TOOL_TIMEOUT):
                await handler(params)
        except TimeoutError:
            return {"status": "failed", "reason": "timeout"}
        except Exception as error:
            logger.warning("Text tool {} failed ({}).", call.name, type(error).__name__)
            return {"status": "failed", "reason": "tool_error"}
        if not results:
            return {"status": "failed", "reason": "no_result"}
        return results[0] if isinstance(results[0], dict) else {"result": results[0]}

    async def _live(self, contents, instruction, declarations, handlers, context, on_text, on_step):
        """Answer through a one-turn Live session, keeping only its output transcript; audio is discarded."""
        tools = [
            {"function_declarations": [{**f, "behavior": "BLOCKING"} for f in tool["function_declarations"]]}
            if "function_declarations" in tool else tool for tool in declarations
        ]
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            output_audio_transcription=types.AudioTranscriptionConfig(),
            system_instruction=instruction,
            tools=tools or None,
        )
        logger.info("Gemini text quotas are used up; answering from {} transcripts.", LIVE_MODEL)
        async with asyncio.timeout(LIVE_REPLY_TIMEOUT), AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(LIVE_CONNECT_TIMEOUT):
                    session = await stack.enter_async_context(
                        self.client.aio.live.connect(model=LIVE_MODEL, config=config))
            except errors.APIError as error:
                if error.code == 429:
                    raise QuotaExhausted(retry_after(error)) from None
                raise
            await session.send_client_content(turns=bounded(flatten(contents)), turn_complete=True)
            # Client content alone did not trigger inference on 3.8.
            await session.send_realtime_input(text=" ")
            text, awaiting = "", False
            while True:
                received = False
                async for message in session.receive():
                    received = True
                    if message.tool_call:
                        responses = []
                        for call in message.tool_call.function_calls or []:
                            result = await self._call(handlers, call, context)
                            on_step(step_label(call.name, call.args, result, self.integrations))
                            responses.append(types.FunctionResponse(id=call.id, name=call.name, response=result))
                        if responses:
                            await session.send_tool_response(function_responses=responses)
                            awaiting = True
                    content = message.server_content
                    if not content:
                        continue
                    if content.model_turn:
                        awaiting = False
                    if content.output_transcription and content.output_transcription.text:
                        awaiting = False
                        text += content.output_transcription.text
                        on_text(text)
                    # A turn that ended on our tool response is still waiting for the model's answer.
                    if content.turn_complete and not awaiting:
                        return text
                if not received:
                    raise ConnectionError("Gemini Live closed the connection")
