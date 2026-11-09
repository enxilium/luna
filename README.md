# Luna

Desktop voice assistant using Gemini 3.8 Live, Pipecat, and native macOS audio.
Gemini handles speech recognition, turn detection, reasoning, tools, and generated
speech over one persistent connection, opened after the local wake word detector
hears "Luna". Persistent memory uses a separate local Hindsight/Ollama service.
VoiceStudio is not needed to run the assistant.

## Run

```sh
uv sync
uv run python -m scripts.setup_wake_word
```

Add your Google AI Studio key to the project's `.env`:

```dotenv
GOOGLE_API_KEY=your-api-key
```

The root `.env` is loaded automatically and ignored by Git. Existing process
environment variables take precedence. `.env.example` contains the required key
name. Gemini API access and billing belong to the key's Google project; MCP
services retain their separate OAuth credentials in Keychain.

```sh
uv run python main.py
```

Allow microphone access for the launching app in macOS Privacy & Security.
Choose input/output devices before starting Luna. Wait for “Waiting for the wake
word,” then say "Luna". You can continue with your request in the same sentence;
audio is buffered locally while Gemini connects. After activation, speak and
interrupt normally. Luna is instructed to recognize natural endings: a closing
"thanks" after a completed request, "nevermind" when abandoning it, or a goodbye.
It calls `end_conversation`, finishes any brief closing reply, then disconnects
Gemini and resumes local wake word listening. Follow-ups such as "thanks, now
turn it down" keep the conversation open; finishing a task alone does not end it.
Say "Luna" again to reconnect; press Ctrl+C to stop the app.
The Mac notch widens sideways on wake, keeping its original height. A moon sits
on the left and five audio bars on the right: white while listening, blue/purple
during Luna's speech. They follow the actual microphone and delivered playback
audio. The indicator collapses after the conversation's closing audio finishes,
stays visible during connection recovery, and never takes keyboard focus.
Hover over the island to open the text composer. Click the field to type; Enter
sends, Shift+Enter inserts a line break. Sending expands it into a scrolling chat
with simulated, streamed replies. Pin keeps the conversation open when you move
away; Escape dismisses it. The history button opens previous chats; the compose
button starts a new one. Chats and their drafts are saved locally across restarts.
Drag files onto the island to queue them for your next message; remove a queued
file with its × button. Originals stay in place. Text and files are not connected
to Gemini, tools, speech, or agent memory yet.
Hold-to-talk shortcuts are not connected yet.

Try the interactive text UI without opening the microphone, connecting to Gemini,
or starting integrations:

```sh
uv run python main.py --preview
```

Hover over the top-center island, click the composer, and send a message. The
preview keeps running until Ctrl+C in the launching terminal.

See [notch UI design](docs/notch-ui.md) for the native window and future trigger seam.

Missing keys or a failed connection cannot declare the assistant
ready. The typed interface connects immediately without wake word detection.

Wake word detection uses sherpa-onnx 2.0.1 with the tested full-precision chunk-8
model and threshold 0.25. The setup command downloads and checks the model once
into `~/Library/Application Support/Luna/models/`. It is already installed on
this machine. Detection runs locally on the existing echo-cancelled audio stream;
no Gemini connection opens before activation. A one-second rolling audio buffer
preserves the wake utterance, and further speech is retained during connection
setup. These buffers stay in RAM and are cleared on activation, conversation end,
or shutdown.

Connection setup has a 10-second deadline per attempt. Temporary connection and
send failures retry with backoff for up to 30 seconds, while microphone audio is
buffered locally. If recovery fails, Luna returns to wake word listening; say
"Luna" again after connectivity returns. Server `GoAway` warnings renew the socket
before its deadline. Expired or rejected resumption handles fall back to a fresh
session with recent conversation history. Recovery resends tool results rather
than repeating completed actions. There is no conversation inactivity timer.

## Persistent memory

Install [Ollama](https://ollama.com/download), then run this once:

```sh
uv run python -m scripts.setup_memory
uv run python main.py
```

Setup installs Hindsight 1.1.0, a dedicated PostgreSQL instance, local embedding
and reranking models, and Qwen 3.8 27B for memory processing. It reuses an existing
matching Ollama model; a fresh download is about 18 GB. This configuration targets
the current 48 GB Apple Silicon Mac. Two macOS login services start and supervise
the stack, including after a restart. Gemini remains Luna's voice model.

Each wake starts a new **conversation**; one app runtime is a **session**.
Luna receives personal notes and recent context at each wake, and can search
older details with `recall_memory`. Ordinary finalized conversation text is saved
automatically. Explicit requests to remember or correct something also use
`remember_memory`. Extraction and note updates run in the background, so recent
transcripts bridge the delay. The typed interface shares the same memory.

Storage and memory processing are local and need no paid memory API. Selected
notes and recall results are sent to Google's Gemini along with the conversation;
its normal API usage still applies. Transcripts are stored as text, without raw
audio, under `~/Library/Application Support/Luna/memory/`. Integration credentials
stay in their existing storage. Without memory setup Luna warns and runs without
persistent memory; after setup it reports storage failures instead of silently
discarding conversation text.

Stop Luna and the typed interface before wiping test data:

```sh
uv run python -m scripts.reset_memory --yes
```

This permanently deletes all Luna transcripts, extracted memories, topic notes,
pending work, and memory-service logs, then starts an empty memory bank. Models,
integration configuration, and sign-in credentials are preserved. The command
refuses to reset an active memory runtime. See [memory design and verification](docs/memory.md).

## Flow

```text
microphone → Apple echo cancellation → local "Luna" detector
                                      → Gemini 3.8 Live after wake
                                      ↔ native MCP tool handlers
speaker ← normal Apple playback       ← Gemini audio
```

Audio streams to Gemini as 16 kHz mono PCM; the generated 24 kHz PCM returns
straight to the speaker. Input/output transcripts come from Gemini. There is no
local STT, separate TTS service, canned acknowledgement, or second conversational
model. Gemini owns turn detection and interruption; Pipecat owns
frames, context, function execution, and session lifecycle.

The Mac transport retains two engines: ordinary speaker playback and a separate
silent engine for microphone echo cancellation. Interruption clears only speech
playback. A stopped echo canceller blocks playback; audio startup and device stalls
trigger a clean audio-pipeline rebuild. A capture watchdog also detects missing
microphone callbacks after sleep or a device interruption. Failed audio startup
retries with backoff; Ctrl+C and termination signals stop those retries cleanly.

## Tools and context

Luna publishes every discovered **allowlisted** MCP tool, with its complete
schema and description, to the Live session. There is no local ranking, tool
search, six-tool budget, operation-help round, prompt-cache management, or special
Docs replacement facade. The recorded inventory has 65 upstream tools across
nine integrations. Connect, authorize, and result-page controls are published too;
Pipecat adds its native authorization-cancel tool.
Voice mode also publishes `end_conversation`, a local tool for returning to wake
word listening without restarting the app or microphone capture.

Canonical argument validation, allowlists, per-integration ordering, authorization,
and cancellation remain in the MCP runtime. Gemini chooses tools directly.
Independent integrations can execute concurrently. Playback IDs must be returned
by Spotify or explicitly supplied by the user. Completed actions are never
replayed by the runtime on login or reconnect.

MCP discovery reads every catalog page. Reconnect, completed authorization and
supported list-change notifications refresh that catalog. Live tool declarations
are fixed at setup in Pipecat 1.12, so actual catalog changes start a new Live
session, reseeding conversation and completed results. Ordinary turns keep the
same connection. Native Pipecat async authorization stays active while the user
speaks, supports explicit cancellation, and returns one final result; Live does
not support intermediate async result streams.

Most schemas use keywords outside Gemini's OpenAPI schema subset. Luna passes
them through Gemini's supported `parameters_json_schema` field. The original MCP
schema remains authoritative at execution time; no constraints are removed to
make a model call work.

Gemini's built-in sliding-window compression manages long conversations; Luna
has no parallel summarization model or custom prompt-cache machinery. Equivalent
text/structured tool results are deduplicated. Responses above 8,192 characters
remain complete in session memory, with a 4,096-character preview and reference.
`read_tool_result(result_id, offset=0, limit=4096)` retrieves bounded pages;
shutdown clears them and expired references return an explicit error. Non-text
MCP content is retained as serialized data; this text interface is not a media
renderer.

## MCP configuration

Configure services in `~/Library/Application Support/Luna/mcp.toml`.
Missing configuration leaves normal conversation available. A failed integration
does not block the other integrations or basic conversation. Pipecat is pinned to
1.12.0 and the MCP SDK to 2.1.1.

```toml
[integrations.calendar]
description = "My calendar"
url = "https://your-calendar-server.example/mcp"
auth = "mcp_oauth"
redirect_uri = "http://127.0.0.1:8765/oauth/callback"
# client_id = "registered-public-client-id"  # Otherwise use SDK client registration.
# scopes = "calendar.read"

[integrations.local]
description = "Local tools"
command = "/absolute/path/to/server"
args = ["--stdio"]
cwd = "/absolute/path/to/server-directory"
auth = "none"
# tools = ["read_item"]  # Optional execution/discovery allowlist.
```

Use one transport per entry: `url` for Streamable HTTP, or `command`, `args`,
and optional `cwd` for stdio. Remote HTTP endpoints must use HTTPS. Authentication
is selected separately: `none`, `mcp_oauth` for HTTP, `google_api` for the local
Workspace backend, or `spotify` for the local Spotify server. IDs must be unique lowercase names starting with a letter,
at most 32 characters, using letters, digits, `_`, or `-`.
Keep secrets out of this file; stdio commands and arguments must also be nonsecret.

`connect_integration` discovers or refreshes schemas. A server protecting discovery
can return `auth_required` before its service tools are available.
Luna explains the need to sign in and calls `authorize_integration`.
The browser opens only during that explicit authorization tool, with the
loopback listener already running. Gemini receives the final outcome and generates
the spoken explanation; intermediate authorization status stays in local logs.

Login stays active while you speak. Duplicate login requests return the current
status. Ask Luna to cancel sign-in to use Pipecat's native
`cancel_authorize_integration` tool. The five-minute deadline, cancellation,
and shutdown close the listener or auth process. Completion refreshes the catalog
before triggering inference. The runtime never replays your original
service action after interactive login; Luna decides whether to retry or ask you.
Terminal outcomes are `connected`, `denied`, `timed_out`, and `failed`;
Pipecat supplies the explicit cancellation notice.

Standard MCP OAuth uses the SDK for discovery, registration, PKCE, token exchange,
issuer binding, and silent refresh. Tokens, absolute expiry, registered client
information, and refresh metadata live in macOS Keychain under `Luna MCP`.
Background connections never open Keychain dialogs. If macOS requires approval,
the service returns `auth_required` with `keychain_access_required`; ask Luna to
authorize that integration. Explicit sign-in and the Google setup script allow
Keychain prompts. Each storage instance reuses its loaded record and updates it
only after a successful Keychain write.
A registered public client's ID and exact redirect can be configured above.
Preprovisioned confidential-client information belongs in
`KeychainTokenStorage.set_client_info`, using the SDK's
`OAuthClientInformationFull`, rather than TOML. Ordinary forbidden responses
and temporary refresh failures do not initiate login; revocation and explicit
scope challenges do. Services with different server-managed login mechanisms
need their own `AuthAdapter`. A login URL alone is insufficient to establish
successful authorization.

### Google Workspace

Workspace defaults to Luna's local MCP backend, which calls the public Gmail,
Drive, Docs, Sheets, Slides, Calendar, Chat, and People APIs. It publishes the same
59 recorded Google tool declarations through the existing MCP runtime. Gemini,
Pipecat, and both conversation entry points use the same flow. Workspace needs no
additional tool allowlist: only these implemented functions exist.

Enable the relevant public APIs in your Google Cloud project, configure OAuth
consent and test users, and create an OAuth **Web application** client. Register:
`http://127.0.0.1:8765/oauth/callback`. Public APIs do not require the hosted MCP
Developer Preview enrollment. Account restrictions, enabled APIs, scopes, and
Workspace administrator policies still apply. `GOOGLE_API_KEY` is for Gemini;
Workspace uses its separate OAuth grant.

Import a downloaded OAuth client JSON:

```sh
uv run python -m scripts.setup_google ~/Downloads/client_secret_YOUR_CLIENT.json
uv run python main.py
```

Setup protects the JSON under `~/Library/Application Support/Luna/`, imports the
client secret into Keychain, and configures eight `google_*` integrations. Existing
tokens and unrelated integrations are preserved. For an existing setup, switch
backends without importing credentials again:

```sh
uv run python -m scripts.setup_google --backend local
# Switch back when Google's hosted servers are available for your project:
uv run python -m scripts.setup_google --backend hosted
```

Restart Luna after switching. Each switch saves a timestamped configuration backup.
The hosted option still requires Google's MCP/API enablement and any current
[Developer Preview enrollment](https://developers.google.com/workspace/preview).
Both backends reuse the original service-bound Keychain records.

The local configuration looks like this; retain your existing OAuth fields:

```toml
[integrations.google_gmail]
description = "Gmail messages and drafts"
backend = "workspace"
service = "gmail"
auth = "google_api"
redirect_uri = "http://127.0.0.1:8765/oauth/callback"
scopes = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose"
```

Authorization remains explicit through `authorize_integration`, with a checked
loopback callback, PKCE, silent refresh, and no automatic action replay. Initial
Calendar scopes permit reads and availability. A supported write scope is requested
only after an explicit insufficient-scope response. Keychain approval can be needed
for a new Python process; normal startup never opens that dialog.

A real Gmail inbox metadata read succeeded through the configured local backend
in 0.58 seconds. See [backend details and measured limits](docs/workspace-backend.md)
for the full verification, API differences, and rollback instructions. Public API
adapters preserve tool contracts, but cannot reproduce every hosted semantic-search,
generated-snippet, or OCR feature.

### Spotify

Create a Spotify developer app and allow `http://127.0.0.1:8888/callback`.
Put these entries in the repository's ignored `.env`:

```dotenv
SPOTIFY_CLIENT_ID=your-client-id
SPOTIFY_CLIENT_SECRET=your-client-secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

```sh
uv run python -m scripts.setup_spotify
uv run python main.py
```

Setup checksum-verifies a separate Node 26.8.1 runtime under Luna's application
data, builds the [Spotify MCP server](https://github.com/marcelmarais/spotify-mcp-server)
at revision `001fea0186ad8f40a0dad7f6f154a47e71962f85` with Luna's playback patch,
and adds a stdio entry. The patch confirms playback and starts named tracks from
their albums to work around Spotify's silent direct-track failures. Subsequent
album tracks may continue after the requested song ends.
It maps the environment names to upstream's `clientId`, `clientSecret`, and
`redirectUri`. A `localhost` redirect is normalized to `127.0.0.1` in the
generated configuration; the developer app must allow that exact address.
Existing credentials and integration configuration are preserved on reruns.

Spotify's credentials stay in its owner-only `spotify-config.json` outside
this repository, under `~/Library/Application Support/Luna/servers/spotify/`.
The adapter runs prebuilt `auth.js` asynchronously, then verifies successful
exit and saved, unexpired credentials. The server reloads tokens on requests
and handles refresh. Its auth output and service-process stderr are not forwarded
to conversation or logs. Setup exposes search, current playback, playlists,
devices, play, and pause; change the `tools` allowlist to select other upstream tools.
The upstream login requests its fixed Spotify scopes, including write access.

Spotify returns text and Markdown, including numbered `ID:` fields, bold track IDs,
plain playback receipts, and errors that sometimes omit the MCP error flag. Luna's
Spotify adapter handles these formats alongside JSON and structured responses.
Music IDs retain their track/album/artist/playlist type; device IDs cannot authorize
music playback. See [format verification](docs/spotify-compatibility.md).

## Typed input

```sh
uv run python -m scripts.inference_tts
```

This opens the existing Mac chat window with the microphone disabled. Typed text
and streamed audio use the same Gemini Live service and MCP runtime. The window
shows Gemini's output transcript; there is no separate inference/TTS stack.

## Verification

```sh
uv run python -m unittest discover -s tests -q
uv run python -m scripts.benchmark_tools --audible --repeats 2 --output /tmp/luna-gemini.json
```

Tests simulate the Live protocol while exercising the real Pipecat connector,
including audio formats, readiness, async login, interruption, explicit cancellation,
catalog refresh, tool ordering, argument validation, allowlists, and result paging.
The benchmark uses the real Gemini model and optional native speaker output, with
recorded full schemas and synthetic service effects. It does not edit external
documents, control real Spotify playback, or open the microphone.

See [Gemini migration and measurements](docs/gemini-live.md) for results and limits.
Older Qwen/VoiceStudio experiments in `docs/benchmarks/` are historical and do not
describe the current pipeline. The old benchmark strategies and local model client
have been removed.

## Optional audio diagnostics

`scripts.audio_compare`, `scripts.tts_alloy`, and `scripts.tts_mlx` remain standalone
VoiceStudio diagnostics for the native audio transport. They are not part of Luna's
Gemini pipeline and require VoiceStudio only when explicitly run.

References: [Google Gemini 3.8 Live](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live),
[Pipecat Gemini Live](https://docs.pipecat.ai/pipecat/features/gemini-live),
[Pipecat service reference](https://docs.pipecat.ai/api-reference/server/services/s2s/gemini-live),
[Apple voice processing](https://developer.apple.com/videos/play/wwdc2023/10235/).
