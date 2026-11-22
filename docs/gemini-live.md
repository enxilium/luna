# Gemini Live migration

Luna uses `gemini-3.8-live` through Pipecat 1.12.0. Gemini receives the
microphone stream, detects turns, chooses functions, and generates the audio
played by the Mac. Typed input (`scripts.inference_tts`) uses the same Live service
code with capture disabled, in its own process and session. The notch text chat
normally uses Flash models instead; it opens a one-turn Live session, keeping only
the output transcript, after both Flash quotas are used up (see README, "Island text chat").
All spoken words come from Gemini. A local sherpa-onnx detector gates voice startup
until "Luna" is heard. There is no acknowledgement generator, local conversational
model, or separate TTS service.

## Runtime

1. Load `GOOGLE_API_KEY` from the environment or the root `.env`. Discover every
   page of each configured MCP catalog concurrently. Unavailable integrations
   do not prevent the others from connecting.
2. Start native microphone capture and local wake word detection. Keep Gemini
   disconnected until "Luna" is heard, then open one persistent Live session.
   Publish every discovered allowlisted tool, its complete description, and its
   JSON schema. Preserve the wake utterance and speech during connection setup
   in bounded RAM buffers. Wait for setup and context seeding before forwarding
   the buffered audio; do not generate an initial greeting. The connection stays
   active until `end_conversation` or shutdown. The local `end_conversation` tool
   requests closure, returns its result so Gemini can finish replying, then waits
   for the final turn and native playback to drain before disconnecting and
   rearming wake word detection while microphone capture stays running.
   The next "Luna" opens another Live session. Typed input connects immediately
   and does not publish this tool.
3. Send microphone PCM at 16 kHz. Gemini's own turn detection drives responses
   and interruption. Send its generated 24 kHz PCM directly to native playback.
   The existing separate capture/echo-cancellation and speaker engines remain.
4. Execute Gemini's function calls through Pipecat and the shared MCP runtime.
   Validate canonical arguments and allowlists before contacting a service.
   Calls within one integration remain ordered; different integrations may run
   concurrently. Send actual results back to Gemini for its spoken answer.
5. Keep authentication in the existing OAuth/Keychain adapters. Login is a
   native nonblocking Pipecat function with explicit cancellation and one final
   result. An interruption does not abandon a pending login. Login or reconnection
   never automatically replays a service action.

Voice instructions make conversation closure a contextual Gemini decision. A
goodbye, dismissal, abandoned request, mistaken activation, or closing "thanks"
after a completed task should call `end_conversation` without asking for an
explicit stop-listening command. Follow-ups, replacement requests, temporary
pauses and gratitude while work remains pending keep the conversation open.
Task completion and silence alone do not close it. Closing an exchange returns
Luna to its normal dormant state, available for new requests via the wake word.
A closing acknowledgement or general offer of future help should accompany
`end_conversation` in the same turn, without waiting for the user to decline
further help. New requests or questions in the latest user turn take precedence
over closing cues. A brief natural closing acknowledgement is optional.
A pending close is cancelled by user interruption or a session disconnect; it
cannot end a later conversation. Forced disconnects for recovery and shutdown
still interrupt speech.
This tool does not undo external actions or guarantee cancellation of work already
started; an available cancellation tool should be used first for abandoned work.

The selector, schema budgets, search/help tools, Docs replacement facade, local
Ollama client, local summarizer, and prompt-cache management are removed.
`connect_integration`, `authorize_integration`, and `read_tool_result` remain;
Pipecat supplies `cancel_authorize_integration`. The recorded inventory produces
**69 declarations: 65 upstream tools and four controls**, about **203 KB** in
the recorded outgoing setup. Both modes add the local `get_weather` tool
(`src/agent/weather.py`), and voice mode adds `end_conversation`.
When persistent memory is set up, both modes also publish `recall_memory` and
`remember_memory`.
Live usage reports roughly 53,000 prompt tokens for
an initial response with this inventory.

Gemini's sliding-window context compression manages the remote conversation.
The runtime still deduplicates equivalent text/structured responses and retains
oversized results in session memory. A preview explicitly marks incompleteness;
`read_tool_result` retrieves the full data in bounded pages. References expire
at shutdown. This storage is for result retrieval, not prompt caching.

## Pipecat compatibility

The shared service in `src/agent/services.py` contains the compatibility code;
both entry points use it. Luna supervises SDK connections and recovery. Pipecat
builds the connection configuration and handles model events, function calls,
audio frames, interruption, and the session history protocol.

- The adapter uses Gemini's `parameters_json_schema` field. The default
  OpenAPI-schema conversion rejects valid upstream MCP keywords such as
  `readOnly`, `deprecated`, and `allOf`. Local `$ref` pointers are expanded for
  the model and an unused `$defs` block is dropped (`src/integrations/schemas.py`).
  Constraints are preserved, and execution still validates the original schema.
- Setup and Pipecat's StartFrame must use the same context, declarations, and
  registered handlers. Without this, advertised tools can lose their handlers
  during startup. A delayed fake connection and real tool calls verify this.
- Voice mode defers the shared connection path until the local audio gate
  activates it. Context and catalog updates cannot open an idle connection;
  activation loads the latest catalog. Cancellation stops both connection
  startup and buffered-audio delivery. The `end_conversation` result goes back
  to Gemini so it can finish its closing turn. After that turn and its playback
  drain, Luna drops any results still pending for the conversation instead of
  sending them, closes the session, clears its resumption handle, and resets the
  local detector. Transport reconnection preserves the current conversation;
  explicit wake activation loads configured memory separately.
- Typed requests are retained in local conversation context and committed with
  `send_client_content(turn_complete=True)`, followed immediately by the Gemini
  3.x realtime trigger. Client content alone did not trigger inference in the
  live test. Pipecat uses the same trigger for seeded initial responses. It is
  protocol input, not a spoken acknowledgement or an inference delay.
- Pipecat 1.12 does not apply live tool-update frames. When the actual catalog
  changes, Luna opens a new session and seeds conversation plus completed
  results before continuing. Results from calls started in the previous session
  are delivered as data rather than replayed as actions. Unchanged catalogs
  keep the existing connection.
- Gemini Live accepts one final async result, not intermediate result streams.
  Authorization progress stays in local logs; Gemini receives the final outcome.

## Background recovery

The connection supervisor covers setup, receive, and send failures. Each setup
attempt has a 10-second deadline. Temporary failures back off from 0.5 seconds
to 4 seconds, within a 30-second recovery budget; a stable connection resets that
budget. Repeated short-lived connections do not reset it. Unrecoverable API access
errors or an exhausted budget return voice mode to local wake word listening.
The next wake starts a new attempt. There is no inactivity timeout.

`GoAway` schedules renewal at a turn boundary or one second before the supplied
deadline. The latest resumable handle is reused for up to two hours after a
disconnect. A non-resumable update discards the older checkpoint; rejected handles
retry without resumption. Fresh connections seed up to 32,000 UTF-8 bytes of recent
whole user turns, so long local transcripts cannot create an unbounded replay.
This seed limit does not delete local history. An oversized single turn may be
omitted from the fresh-session seed.

The microphone gate buffers new speech during recovery. Unsent audio and gate
buffers are bounded to 32 seconds each and are discarded when returning to wake
word listening. Already delivered audio can still be lost at a server checkpoint;
resumption is not a guarantee of lossless audio. Tool results finishing during a
disconnect are delivered after recovery. Repeated call IDs reuse the pending or
completed result without executing the action again.

Native capture is checked once per second. Stopped engines, five seconds without
microphone callbacks, or a scheduling gap after sleep cancel and release the
audio pipeline. The background runner rebuilds it with backoff from one to 30
seconds, preserving the
shared MCP runtime. This also covers sleep/wake interruptions that leave native
audio stopped. SIGINT and SIGTERM cancel active work and retry waits.

These hooks use private Pipecat methods, so **Pipecat is pinned to 1.12.0**.
Run the connector and authorization tests before changing that version. The
Google SDK is resolved by `uv.lock` (2.34.0 for this verification).

## Verification

```sh
uv run python -m unittest discover -s tests -q
LUNA_LIVE_TESTS=1 uv run python -m unittest tests.test_mcp_pipeline.LivePipelineTests -q
uv run python -m scripts.benchmark_tools --audible --repeats 2 \
  --cases greeting,calendar,docs,discovery,playback,history_playback,spotify,large_result,explicit_playback,multi_service,unsupported \
  --timeout 30 --output /tmp/luna-gemini.json
```

The offline suite exercises both production entry points and the real Pipecat
connector against a fake Live session. It covers audio formats, readiness errors,
handler registration, schema refresh, login interruption/cancellation, argument
validation, allowlists, ordered execution, catalog pagination, result retrieval,
and explicit or service-resolved Spotify identifiers.

The benchmark uses the **real Gemini service**, full recorded schemas, synthetic
MCP effects over local HTTP, typed prompts, and optional native Mac speaker
output. It never captures the microphone or edits external services. Each trial
opens a fresh Live session, with historical messages explicitly seeded for the
history case. Trials run sequentially; this is not a randomized production load
test or a 100-turn promotion gate.

The runner records generated audio, speaker delivery, first native render
callback, final tool-result receipt, task completion, full spoken transcript,
function arguments, outgoing schema sizes, and provider usage. The render callback
is a software proxy for speaker output, not an acoustic measurement of useful
speech. Usage counters are reported as supplied by Pipecat; cache counters can
be unavailable. Summed prompt usage across function rounds is not the number of
unique context tokens.

Tool completion is separate from finishing the spoken answer. A ten-second
spoken list can have completed its service request in under a second. Conversely,
a fast acknowledgement does not establish successful tool execution. Missing
audio, wrong actions, and timeouts remain failures. A benchmark-only five-second
grace after a text-only turn boundary observes late audio; it never retries a
request or changes the production pipeline.

## Measurements and limits

Final measurements and machine/source metadata are recorded in
[`benchmarks/gemini-live/`](benchmarks/gemini-live/). Failed experiments are kept
alongside successful checks. Earlier October Qwen/VoiceStudio reports describe
the old implementation and are not current Gemini measurements.

On November 1, 2026, with an M5 Pro Mac, the mixed run measured:

| Measurement | Result |
| --- | --- |
| Mixed trials | 22 across 11 cases |
| First native speaker render, median | 1.59 s |
| First native speaker render, empirical p95 | 2.73 s |
| First native speaker render, range | 0.72–5.47 s |
| Speaker output below two seconds | 17/22 |
| Fresh session readiness, before submission | 0.52–1.17 s |
| Original automated task checks | 18/22 |
| Task checks after fixing two refusal-checker false negatives | 20/22 |

Both remaining failures were the document prompt's ambiguous synthetic identifier:
Gemini searched for a file named `doc-example` instead of applying the edit. The
original prompts remain in `mixed-prompts.json` and the raw failures in
`mixed.json`; source hashes are recorded separately. The original checker also incorrectly rejected two correct
unsupported-action refusals phrased “I don't have the ability.” The checker now
accepts that wording; the archived raw run has not been rewritten.

A separate four-trial follow-up explicitly used `documentId=doc-example` and
rechecked unsupported actions. It passed **4/4**. The two document edits each
performed one exact, case-sensitive replacement followed by a verification read.
Their final results returned in **4.45 s** and **5.01 s**; first speaker output
arrived in **5.06 s** and **5.84 s**, and the spoken answer finished in **11.31 s**
and **10.50 s**. Across correctly completed service tasks in these two runs,
final tool-result times ranged from **0.52–5.01 s**. These use synthetic service
effects and do not establish a ten-second bound for real external services.

**The two-second speech target is not met consistently.** Publishing all tools
works, but cloud execution does not eliminate provider variability or incorrect
resource interpretation. No acknowledgement, replay, or retry conceals these
misses. There is no contemporaneous Qwen comparison or 100-turn promotion claim.

The final offline run passed **60 tests**, with one opt-in test skipped. Running
that real-Gemini authorization test separately passed, including schema refresh,
exactly one login, and exactly one private service read. Dependency lock
validation, compilation, and focused unused/undefined-name checks also passed.

The live trigger smoke check passed greeting and calendar correctness. First
native render callbacks were **0.75 s** and **1.46 s** after typed submission;
the calendar result returned in **0.76 s**. These two trials alone do not
establish a latency guarantee.

Two earlier experiments are retained independently: client-content-only typed
turns produced no inference until the realtime trigger was restored; an
interrupted run using Pipecat's native realtime text path included a stall after
a successful calendar tool result. Their failures are not mixed into the final
configuration's statistics or silently counted as successful trials.

The service runs in Google's cloud. An API key and network access are required;
microphone audio, transcripts, tool definitions, and tool results are sent to
Google. MCP authorization remains separate from the Gemini key. Real provider
latency and real external service latency are variable. The tests do not
establish current Workspace preview access, actual Spotify playback, or live
microphone turn-detection/echo-cancellation behavior.

References: [Gemini 3.8 Live model](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live),
[Pipecat guide](https://docs.pipecat.ai/pipecat/features/gemini-live),
[Pipecat service reference](https://docs.pipecat.ai/api-reference/server/services/s2s/gemini-live),
[Live API tools](https://ai.google.dev/gemini-api/docs/live-api/tools).
