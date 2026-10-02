# Luna

Local voice assistant using Python 3.14, Pipecat, Ollama, and VoiceStudio.
Run `main.py` to talk through the Mac's default microphone and audio output.

```text
src/
  agent/
    conversation.py  # Pipecat pipeline, turn detection, and audio transport
    services.py      # Local Ollama and VoiceStudio service connections
  clients/
    ollama.py        # Standalone text inference
    voicestudio.py   # Standalone TTS/STT requests
  audio/
    apple.py         # Native macOS capture/playback and microphone echo cancellation
  integrations/
    mcp.py           # SDK connections, discovery, and Pipecat tools
    auth.py          # MCP OAuth, browser callbacks, and Keychain
    spotify.py       # Upstream Spotify authorization script
```

## Talk to Luna

Sync Python dependencies on your Mac:

```sh
uv sync
```

Keep Ollama running at `http://127.0.0.1:11434` with
`qwen3.8:27b-q4_K_M` installed. Keep VoiceStudio running at
`http://127.0.0.1:3900` with MLX-Audio available and MLX Whisper selected for STT.
Install `mlx-community/whisper-large-v3-turbo` in VoiceStudio's Model Catalogue.
The voice and inference stack needs no cloud service or API key. Optional
integrations contact the services you configure.

VoiceStudio's startup preferences at
`~/Library/Application Support/OmniVoice/prefs.json` pin the ASR engine and
checkpoint:

```json
{
  "asr_backend": "mlx-whisper",
  "env.OMNIVOICE_ASR_BACKEND": "mlx-whisper",
  "env.ASR_MODEL": "mlx-community/whisper-large-v3-turbo"
}
```

These are entries in the existing preferences file, not a replacement for it.
The engine pin prevents the saved performance profile from restoring CPU ASR.
Restart VoiceStudio after changing its startup preferences. The model weights
live in VoiceStudio's Hugging Face cache; Luna needs no additional dependencies.

```sh
uv run python main.py
```

Allow microphone access when macOS asks. If needed, enable it for the app
launching Python under **System Settings → Privacy & Security → Microphone**.
Speaker playback uses an ordinary `AVAudioEngine`. A separate native engine
provides microphone echo cancellation and noise suppression before Pipecat
detects speech, with its own output running silently. You can use the Mac's
speakers and still interrupt Luna. Choose your input and output devices in
macOS Sound settings before launching; restart Luna after changing devices.

Start speaking when the terminal says "Luna is listening." Pause at the end
of your turn to receive a spoken reply. You can interrupt Luna by speaking
again. Press **Ctrl+C** to stop and release the audio devices.

The conversation flow is:

```text
microphone → Apple voice processing → VoiceStudio STT → conversation context → Ollama
           → VoiceStudio TTS → audio output → assistant context
```

Pipecat's local Silero VAD and Smart Turn model determine turn boundaries.
Its context aggregators retain conversation history for the current session
and handle interruptions. STT sends detected speech segments to VoiceStudio.

The Mac-only audio module uses PyObjC to call the native APIs. The capture
engine uses matching mono I/O formats at the microphone's native rate, with
cleaned input resampled to 16 kHz. VoiceStudio's 24 kHz mono output passes
through the normal playback mixer and device format. Speech never passes
through the capture engine's processed output; macOS supplies system playback
as the echo reference. Microphone AGC is disabled and other-audio ducking is
set to static minimum, matching the configuration verified on this Mac. Native
render completion paces playback with one chunk scheduled ahead to avoid gaps.
Interruptions clear only the speech player without restarting the echo canceller. Startup
fails if native processing cannot be enabled; it does not fall back to an
unfiltered microphone. A stopped echo canceller blocks further playback, and
a stalled audio device cancels the conversation. Shutdown stops both engines,
removes the microphone tap, disables processing, and detaches both players.

The two-engine investigation retained normal speaker sound in the user's
listening check. Two plays of Luna's clip produced no false Silero speech
starts, while real user speech during playback remained detectable and was
transcribed locally. The integrated transport's checks cover echo-only playback,
interruption recovery, and device cleanup. These are short checks on this Mac,
not a guarantee for every volume, room, accessory, or prolonged conversation.
The diagnostic microphone recordings do not establish calibrated echo attenuation.

References: [Apple voice processing](https://developer.apple.com/videos/play/wwdc2023/10235/)
and [Apple's AVAudioEngine sample](https://developer.apple.com/documentation/avfaudio/using-voice-processing).

The local STT latency budget is 10 seconds with a 15-second turn watchdog;
final transcripts trigger replies immediately without waiting out these budgets.
These are safety limits, not fixed delays. A 3.73-second synthesized test clip
took 0.20 seconds to transcribe with warm MLX Whisper, compared with about
4.7 seconds on the previous CPU Faster Whisper engine. The first GPU request
after restart took 2.04 seconds to load the model. These timings cover STT only;
turn detection, Ollama, and TTS still contribute to the full response time.

TTS sends each completed sentence as Ollama streams text; Pipecat queues
audio for playback while subsequent sentences synthesize. VoiceStudio still
finishes a sentence before returning its audio. First replies can be slower
while models warm up. The TTS audio idle timeout allows up to 120 seconds for
batch synthesis; successful replies finish without waiting for that timeout.

INFO logs show each `LLM inference` starting, its first text, and its total
duration. Each sentence's `TTS synthesis` logs its start, first audio, and total
duration. These appear in both voice and typed conversations. LLM and TTS work
can overlap; synthesis timings measure the service request, excluding playback.

Luna explicitly selects VoiceStudio's `mlx-audio` engine for TTS, with `alloy`
selecting that engine's default voice. STT explicitly requests `mlx-whisper`,
which must also be active in VoiceStudio. This prototype does not select saved
voice profiles.

Pipecat's Ollama connector uses the OpenAI-compatible endpoint with thinking
disabled. Its context size comes from Ollama's server/model settings; it does
not inherit the standalone client's 8K setting. Luna keeps history in memory
only. The voice entry point loads configured MCP integrations at startup.

The integration was verified with three spoken inputs through the real local
services, conversation recall, sentence synthesis during playback, voice
interruption clearing queued audio, native Mac speaker writes, and Ctrl+C
shutdown. Temporary verification scripts and audio were removed.

## MCP integrations

Luna uses Pipecat 1.12.0 with its MCP extra and the pinned MCP SDK 2.1.1.
Configure services in `~/Library/Application Support/Luna/mcp.toml`.
An absent configuration leaves the basic voice conversation available.
Invalid entries and connection failures do not stop conversation.

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
# tools = ["read_item"]  # Optional allowlist; omit to publish all discovered tools.
```

Use one transport per entry: `url` for Streamable HTTP, or `command`, `args`,
and optional `cwd` for stdio. Remote HTTP endpoints must use HTTPS. Authentication
is selected separately: `none`, `mcp_oauth` for HTTP, or `spotify` for the local
Spotify server. IDs must be unique lowercase names starting with a letter,
at most 32 characters, using letters, digits, `_`, or `-`.
Keep secrets out of this file; stdio commands and arguments must also be nonsecret.

Discovered tools are namespaced, such as `calendar__list_events`.
`connect_integration` discovers or refreshes schemas. A server protecting discovery
can return `auth_required` before its service tools are available.
Luna explains the need to sign in and calls `authorize_integration`.
The browser opens only during that explicit authorization tool, with the
loopback listener already running. Progress and completion are internal tool
results; Luna's narration is what reaches sentence TTS.

Login stays active while you speak. Duplicate login requests return the current
status. Ask Luna to cancel sign-in to use Pipecat's native
`cancel_authorize_integration` tool. The five-minute deadline, cancellation,
and shutdown close the listener or auth process. Completion publishes refreshed
tools before triggering inference. The runtime never replays your original
service action after interactive login; Luna decides whether to retry or ask you.
Terminal outcomes are `connected`, `denied`, `timed_out`, and `failed`;
Pipecat supplies the explicit cancellation notice.

Standard MCP OAuth uses the SDK for discovery, registration, PKCE, token exchange,
issuer binding, and silent refresh. Tokens, absolute expiry, registered client
information, and refresh metadata live in macOS Keychain under `Luna MCP`.
A registered public client's ID and exact redirect can be configured above.
Preprovisioned confidential-client information belongs in
`KeychainTokenStorage.set_client_info`, using the SDK's
`OAuthClientInformationFull`, rather than TOML. Ordinary forbidden responses
and temporary refresh failures do not initiate login; revocation and explicit
scope challenges do. Services with different server-managed login mechanisms
need their own `AuthAdapter`. A login URL alone is insufficient to establish
successful authorization.

### Google Workspace

Google's remote MCP servers use the existing `MCPOAuthAdapter` over Streamable
HTTP. Follow the [Google setup guide](https://developers.google.com/workspace/guides/configure-mcp-servers)
to enable the service and MCP APIs, configure consent and test users, and create
an OAuth **Web application** client. Register this exact redirect:
`http://127.0.0.1:8765/oauth/callback`.
Your Cloud project must also be enrolled in the
[Workspace Developer Preview Program](https://developers.google.com/workspace/preview).
Successful OAuth or public tool discovery does not establish preview access.
Signing into `gcloud` alone does not authorize Luna to access Workspace data.

Download the client JSON, then import it:

```sh
uv run python -m scripts.setup_google ~/Downloads/client_secret_YOUR_CLIENT.json
uv run python main.py
```

Setup moves the JSON to the owner-only
`~/Library/Application Support/Luna/google-oauth-client.json`, imports the client
secret into macOS Keychain for each server, and adds these integrations to
`mcp.toml`: `google_gmail`, `google_drive`, `google_docs`, `google_sheets`,
`google_slides`, `google_calendar`, `google_chat`, and `google_people`.
Existing integrations and tokens are preserved. Only public client identity,
redirect, scopes, and authorization options appear in TOML.

Each service starts with the scopes in Google's guide; Calendar starts with
read and availability access. The `scopes` entry controls initial consent, and
explicit server scope challenges can request additional access. Google's
`authorization_params = { access_type = "offline", prompt = "consent" }`
requests a refresh token for silent renewal. An OAuth app in testing can have
short-lived refresh authorization; Luna follows the normal sign-in flow when
Google revokes or expires it. Each integration keeps its own server-bound
Keychain record and authorizes lazily when its tools need access.

For example, ask Luna to check Gmail. Google exposes schemas publicly, but an
unauthorized service call returns `auth_required`. Luna narrates sign-in,
opens the browser through `authorize_integration`, and receives completion
without another user message. Completion refreshes tools; the model then
chooses its next call. The runtime never replays the original action.

The adapter normalizes Google's published root-issuer trailing-slash mismatch
at Google's protected-resource metadata endpoint. SDK issuer validation remains
strict for all other providers, and the SDK still owns PKCE and token exchange.
The configured client and callback, real Gmail sign-in, Keychain persistence,
and silent refresh were verified. A real Gmail tool call currently reports that
the Cloud project needs preview enrollment; no service action succeeded.

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
data, builds the unchanged [Spotify MCP server](https://github.com/marcelmarais/spotify-mcp-server)
at revision `001fea0186ad8f40a0dad7f6f154a47e71962f85`, and adds a stdio entry.
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

### Verification

```sh
uv run python -m unittest tests.test_integrations tests.test_google_setup tests.test_mcp_pipeline -v
LUNA_LIVE_TESTS=1 uv run python -m unittest tests.test_mcp_pipeline.PipelineTests.test_live_qwen_and_sentence_tts -v
```

The default suite uses local mock OAuth/MCP servers and stdio processes. It covers
protected discovery, publication, refresh across restart, rotation, revocation,
scope challenges, permission and temporary errors, login deduplication, callback
validation, timeout, cancellation, and shutdown. A running Pipecat pipeline
verifies background completion, interruption, explicit cancellation, and TTS
separation without application-level action replay.
Google-specific checks cover pre-registered confidential clients, public
discovery, offline authorization, configured scopes, credential import,
issuer compatibility, and preservation of existing credentials on setup reruns.
The opt-in check uses real local Qwen and VoiceStudio against mock private data;
it does not open the microphone or play sound. Native Keychain persistence and
one real Spotify login followed by a read-only playlist call were also verified.

## Typed inference and speech test

Keep Ollama and VoiceStudio running, then open the Mac chat window:

```sh
uv run python -m scripts.inference_tts
```

Type a message and press Enter or click Send. Replies stream into the window
and play aloud through the same Ollama, Pipecat sentence synthesis, MLX-Audio,
and speaker playback used by the voice agent. Conversation history stays in
memory while the window is open. Send becomes available after the spoken reply
finishes; this test accepts one message at a time.

The test never opens the microphone or runs STT, VAD, turn detection, or echo
cancellation. Close the window to cancel pending work and release playback.

## Audio output comparison

With VoiceStudio running, compare the current native audio paths:

```sh
uv run python -m scripts.audio_compare
```

Close Luna and other microphone apps first. Choose the output and microphone
in macOS Sound settings and keep devices and volume fixed for the run. Use
**Standard** microphone mode in Control Center to match the verified
configuration. The script uses the current default devices and
stops if they change. It does not select or require a particular device.

One MLX-Audio clip is synthesized and reused across four tests:

| Cases | Comparison | What it separates |
| --- | --- | --- |
| 01 | Normal playback, microphone closed | Listening reference |
| 02 | Normal playback with separate native echo cancellation | Speaker quality and echo-only VAD behavior |
| 03 | Queue cancellation followed by playback, with capture kept alive | Interruption recovery and capture continuity |
| 04 | Normal playback again after cleanup | Recovery control |

After each successful playback, enter `g` (good), `d` (degraded), or `u`
(unsure), optionally followed by a note, such as `d quieter and muffled`.
Each native test runs in a separate process, so unsupported configurations
or CoreAudio failures are recorded and the remaining tests can continue.
Press **Ctrl+C** to stop; completed results remain saved.

The terminal prints a report directory under `diagnostics/audio/`. It contains
`report.md` and `report.json`, the original clip, per-case logs and device
snapshots, plus recordings from the playback mixer and processed microphone. Measurements
include level, clipping, spectral bands, channel/sample-rate formats, render
continuity, device volume, microphone mode, bypass, AGC, and ducking settings.
The microphone recordings are replayed through Luna's default Silero VAD;
remain quiet to use speech starts as an echo check. No STT or conversation
runs. Diagnostic files are excluded from Git.

Send the report after the run. Its findings combine your ratings with the
measurements to locate the responsible layer. Mixer recordings cannot measure
the speakers' acoustic output or identify an undocumented macOS filter.
The previous shared voice-processing playback modes have been removed.

For a silent configuration check, use `--prepare-only` (no synthesis or
playback). `--self-check` checks metering and interpretation without opening
audio devices. `--no-prompts` plays the cases without collecting ratings.
`--source-pcm PATH` reuses an existing 24 kHz mono int16 clip without synthesis.

Run the audio ownership and playback regression checks without opening devices:

```sh
uv run python -m unittest tests.test_apple_audio -q
```

## Ollama

`src.clients.ollama.stream_response(text)` yields response text chunks from
`qwen3.8:27b-q4_K_M`. Thinking is disabled and context is limited to 8K.
Each call is independent; the voice pipeline manages its own history.

## VoiceStudio

`src.clients.voicestudio` exposes two async functions:

- `text_to_speech(text, voice="default")` returns WAV audio bytes. `voice` can
  also be an engine preset or a saved VoiceStudio profile ID.
- `speech_to_text(audio)` transcribes WAV audio with MLX Whisper and returns text.

Standalone TTS uses VoiceStudio's active TTS engine through the local `tts-1`
alias. STT requests the same `mlx-whisper` engine as the voice pipeline and
rejects a different active engine instead of silently using CPU ASR. No API key
is needed for the local endpoints.

```python
import asyncio

from src.clients.voicestudio import speech_to_text, text_to_speech


async def example():
    audio = await text_to_speech("Hello, I am Luna.")
    print(await speech_to_text(audio))


asyncio.run(example())
```

Empty inputs raise `ValueError`. Connection, HTTP, and timeout errors propagate
to the caller. These standalone functions make one request per call. The voice
agent uses Pipecat's native service connectors so streaming, turn management,
and playback stay within Pipecat.
