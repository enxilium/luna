# AGENTS.md: Luna

Luna is a macOS desktop voice assistant. A local sherpa-onnx detector hears "Luna", then one
Gemini Live session (`gemini-3.8-live`, via Pipecat) handles speech recognition, turn detection,
reasoning, tool calls and spoken replies. Tools come from MCP servers (Google Workspace, Spotify,
any configured server). Persistent memory is a separate local Hindsight + Ollama stack.

**Code is the source of truth.** If a doc disagrees with the code, fix the doc in the same change.

## Commands

```sh
uv sync                                           # Python 3.14 (.python-version); never pip install into .venv
uv run python main.py [--demo] [--home DIR]       # all of Luna: notch, wake word + voice, dashboard (demo = seeded dashboard data;
                                                  #   --home = a separate data folder + Keychain key, e.g. to try first-run setup)
uv run python main.py --preview                   # notch UI only: no mic, Gemini, MCP, memory or dashboard
uv run python main.py --dashboard [--demo]        # the dashboard alone, shown at once (development)
uv run python -m scripts.build_app                # build/Luna.app: all of Luna as one agent app (runs this checkout; development)
uv run python -m scripts.package_app [--with-google] [--with-spotify] [--with-model] [--notarize PROFILE]
                                                  # build/package/Luna.app + DMG: self-contained (Python, packages, uv, Ollama,
                                                  #   Node, prebuilt Spotify server); the --with flags bundle your OAuth apps / memory's model
uv run python -m src.dashboard.server --demo --port 8790 --token dev   # dashboard UI in a browser
uv run python -m scripts.inference_tts            # typed chat window → real Gemini + MCP, mic disabled
uv run python -m scripts.sync_canvas              # Quercus assignments → course tasks (needs setup_canvas)
uv run python -m unittest discover -s tests -q    # offline suite (fake Gemini Live server)
uv run python -m unittest tests.test_session_recovery -q   # one module
LUNA_LIVE_TESTS=1 uv run python -m unittest tests.test_mcp_pipeline.LivePipelineTests -q   # real Gemini, needs key
uv run python -m scripts.benchmark_tools --audible --repeats 2 --output /tmp/luna-gemini.json  # real Gemini, synthetic tools
```

- Tests need macOS: they import PyObjC/AVFoundation. A Linux sandbox can read code but cannot run them.
- Memory DB tests are opt-in: `LUNA_MEMORY_TEST_DATABASE_URL=postgresql://hindsight:hindsight@127.0.0.1:55439/luna_memory_tests`
  (the database name must end in `_tests`; the tests truncate its tables).
- Lint: ruff 0.17 has been used (`.ruff_cache`), but there is no committed config and ruff is not a dependency.
- Setup lives in `src/install/` (memory, Google, Spotify, Canvas), run from the app's setup screen or the
  `scripts/setup_*` wrappers. It changes the user's machine. Don't run it unless asked: `setup_wake_word`, `setup_memory`
  (LaunchAgents, ~18 GB model), `setup_google <client.json>` (Keychain, deletes the source JSON),
  `setup_google --backend local|hosted`, `setup_spotify`, `setup_canvas` (installs canvas-mcp 1.18.0, Keychain token). **`reset_memory --yes` permanently deletes all memory.**

## Scope and pinned versions

- macOS on Apple Silicon only (Spotify setup refuses anything else). No Windows/Linux work.
- Pins that matter: `pipecat-ai[google,mcp]==1.12.0`, `mcp==2.1.1` (protocol `2026-07-28`), `sherpa-onnx==2.0.1`,
  `hindsight-client==1.1.0` (service `hindsight-api-slim==1.1.0` in its own Python 3.13 venv), google-genai 2.34.0 (via uv.lock).
- **Don't bump Pipecat casually.** `src/agent/services.py` overrides about 30 private members of
  `GeminiLiveLLMService` (`_connect`, `_connection_task_handler`, `_handle_session_ready`, `_tool_result`,
  `_completed_tool_calls`, `_tool_call_id_to_name`, `_ready_for_realtime_input`, …). Before upgrading, diff
  `.venv/lib/python3.14/site-packages/pipecat/services/google/gemini_live/llm.py` and run the full suite.
  Pipecat 1.15 exists; whether it fixes the issues worked around below is unverified.
- `httpx2`, `numpy`, `loguru` and `google-genai` are imported directly but only arrive transitively.
  If you change dependencies, declare them.

## Repository map

| Path | Role |
| --- | --- |
| `main.py` | Argparse → `src.ui.app.run_app(preview=…, demo=…)`, or `run_dashboard` for `--dashboard` |
| `src/ui/app.py` | Luna as one app: AppKit (notch + dashboard window) on the main thread; voice pipeline on thread `luna-agent` running `asyncio.run` (it waits in `_wait_for_setup` until `voice_ready()`, answering chats with what's missing); the dashboard server on its own thread; `LunaApp` is the app delegate (quit stops everything; reopen (Dock click) shows the dashboard); single-instance locks |
| `scripts/build_app.py` | `build/Luna.app`: a regular (Dock) app, a native executable embedding `.venv`'s Python, every privacy reason in Info.plist; signed with a Developer ID / Apple Development identity if valid (`LUNA_SIGN_IDENTITY`), else ad-hoc |
| `src/agent/conversation.py` | Builds the voice pipeline; `_run_background` rebuilds it after audio failures (1→30 s backoff) |
| `src/agent/services.py` | `_GeminiLiveService`: connection supervision, recovery, wake gating, `end_conversation`, tool catalog refresh |
| `src/agent/text.py` | `TextAgent` for notch chats: Flash-Lite / Flash (brain mode) with a manual tool loop, quota fallback to Live transcripts |
| `src/audio/apple.py` | `AppleAudioTransport`: playback engine plus a separate voice-processing (echo-cancelling) capture engine |
| `src/audio/wake_word.py` | `SherpaDetector` (keyword spotter) and the `WakeWordGate` frame processor |
| `src/audio/levels.py` | Five RMS levels per frame for the notch visualizer |
| `src/integrations/mcp.py` | `MCPRuntime`: config, connections, discovery, tool publication, execution, auth, result paging |
| `src/integrations/auth.py` | MCP OAuth (SDK `OAuthClientProvider`), loopback callback, `KeychainTokenStorage` |
| `src/integrations/schemas.py` | Canonical argument validation (JSON Schema 2020-12) and `$ref` expansion for the model |
| `src/integrations/spotify.py` | Spotify text/Markdown result parsing, playback-ID guard, `SpotifyAuthAdapter` |
| `src/integrations/workspace/` | In-process MCP servers over public Google APIs; `tools.json` holds 59 recorded declarations |
| `src/agent/weather.py` | `get_weather` tool (voice and text): Open-Meteo forecast for the dashboard's saved place or a named one |
| `src/paths.py` | `HOME` (`LUNA_HOME`, default `~/Library/Application Support/Luna`): every data path derives from it; `PACKAGED` (code runs from `Luna.app/Contents/Resources/app`) and `keychain_service()` give a development checkout its own Keychain services (`Luna (dev)`, `Luna MCP (dev)`, `Luna Dashboard (dev)`) over the same data folder; `PROFILE` ("" or a hash for a separate home) tags memory's ports, LaunchAgent labels, Postgres name and Keychain accounts (`Luna`, `Luna MCP`, `Luna Dashboard`) so a test profile never touches yours. Dependency-free (the memory service imports it) |
| `src/resources.py` | Where bundled tools and OAuth apps are: `Luna.app/Contents/Resources/{bin/uv, ollama/, node/, servers/spotify, credentials/}` when packaged, else PATH and your own Luna's files |
| `src/install/` | Installers as `Job`s (step, progress, reason codes; success bumps `generation()` so voice restarts with new integrations): `memory.py` (uv venv + Hindsight, Ollama LaunchAgents, model pull over Ollama's API), `google.py` (client import, one sign-in for all eight services), `spotify.py` (prebuilt or built server, sign-in), `canvas.py` (canvas-mcp, school address + token, read-only) |
| `scripts/package_app.py` | Self-contained `Luna.app` + DMG (`build/package/`); the launcher embeds the bundled CPython 3.14 with an explicit search path (stdlib, `site-packages`, `app`) and never writes bytecode; strips AppleDouble `._*` files, signs every Mach-O inside-out (hardened runtime + entitlements with a Developer ID), notarizes on request |
| `src/setup.py` | First run: Gemini key (env → Keychain `Luna`/`gemini-api-key` → `.env` for scripts only: `run_app`/`run_dashboard` set `DOTENV = False`; a separate home uses only its own Keychain account), key check with Google (header, never URL), microphone, wake model download; `voice_ready()` gates the voice thread |
| `src/memory.py` | Postgres turns and outbox → Hindsight retain; startup notes; `recall_memory`/`remember_memory` |
| `src/ui/notch.py`, `glass.py`, `chat.py`, `chats.py`, `motion.py`, `drop.py` | Notch island, glass layers + Diamond Ring mark + corona, text chat, SQLite chat history, spring animation, file drops |
| `scripts/` | Setup, reset, memory service launcher, benchmark, typed window, VoiceStudio-only audio diagnostics |
| `scripts/patches/spotify-playback.patch`, `spotify-pkce.patch` | Patches applied in order to the pinned upstream Spotify MCP server (playback guards; PKCE sign-in and secret-free refresh) |
| `scripts/fixtures/` | Recorded MCP catalog (65 tools), 180 Spotify response envelopes, Workspace API fakes |
| `docs/` | Design and verification notes; `docs/benchmarks/` holds raw historical results (never rewrite them) |
| `src/dashboard/` | Personal dashboard: `window.py` (glass overlay window, drag strip), `hotkey.py` (global shortcut), `calendar.py` (EventKit events), `location.py` (CoreLocation, town-level), `weather.py` (Open-Meteo), `picks.py` (daily task picks via Gemini Live), `server.py` (loopback aiohttp, token cookie, SSE), `store.py` (SQLite: projects, courses, tasks, notes, events…), `notes.py` (Markdown note files and media), `messages.py` (iMessage `chat.db` + Beeper), `projects.py` (folder scan), `imports.py` (Health, CSV), `web/` (no-build ES modules; `glass.js` is the window bridge, `work.js` the shared task/note pieces), `editor/` (CodeMirror + KaTeX source, built into `web/vendor/editor.js`) |
| `src/clients/voicestudio.py` | Legacy local TTS/STT client, used only by the optional audio diagnostics |

## Runtime architecture

Voice pipeline (`build_worker`):
`AppleInput → WakeWordGate → user aggregator → _GeminiLiveService → AppleOutput → assistant aggregator`
Audio goes in at 16 kHz mono s16le and comes back at 24 kHz. `PipelineWorker` uses `enable_rtvi=False`,
`idle_timeout_secs=None`, `ProcessorUnusablePolicy.CANCEL`, and `app_resources=MCPRuntime`.

Conversation lifecycle (voice, `defer_connection=True`):
1. Gate state `waiting`: keep a 1 s rolling buffer and run the detector in `asyncio.to_thread`. Gemini is disconnected.
2. "Luna" heard → `on_activity(True)` (notch expands) → `llm.activate()`: with memory, load notes into the system
   instruction and clear the context; then connect and wait for readiness. Speech arriving meanwhile is buffered
   (32 s cap). Without memory the context is *not* cleared, so the previous wake's history is reseeded.
3. `on_connection_ready` → `gate.resume()` flushes the buffer and starts streaming. `on_connection_lost` → `gate.pause()`.
4. Gemini calls the local `end_conversation` tool → `_end_requested`. After `turn_complete`, `flush_pipeline()` drains
   playback, then `_end_conversation()` disconnects, drops the resumption handle and pending results, and fires
   `on_conversation_ended` → `gate.rearm()`. Microphone capture keeps running throughout.
5. A user interruption, typed text, or a disconnect cancels a pending end.

Typed mode (`scripts/inference_tts.py`) uses `build_llm_service(defer_connection=False)`: it connects on start,
has no gate and no `end_conversation`, and keeps one memory conversation per process.

Notch text chat (`TextAgent`, started by `run_conversation(text_requests=…)` beside the voice loop):
- UI → `TextRequest` on a thread queue → `TextAgent.serve` → `TextEvent`s back through the UI event queue. SQLite stays on
  the main thread. Each request is stateless: the UI sends prior completed turns; failed replies are left out.
- Brain off: `FAST_MODEL` (`gemini-3.5-flash-lite`; Google has no 3.8 Flash-Lite text model) then `BRAIN_MODEL`
  (`gemini-3.8-flash`); brain on reverses that order. A 429 marks the model unavailable for its `retryDelay` (default 60 s).
  With both unavailable, a one-turn `gemini-3.8-live` session answers with BLOCKING tools; audio is dropped and its
  output transcription is the reply.
- Tools are called directly with hand-built `FunctionCallParams` (`llm`/`pipeline_worker` are `None`). Switching
  models mid-turn flattens that turn's calls and results to text (thought signatures are model-specific), so nothing is replayed.
- The UI never shows model or provider names; `TextEvent.model` is stored but only logged. Errors are plain language.
- The local Qwen model is for memory only. A Qwen text path was tried and dropped: the full tool catalog is ~50k
  tokens (over its 32k window), prefill is ~350 tokens/s, and it competes with Hindsight on one Ollama slot.
- Memory conversation ID = chat ID. Startup notes are loaded once per chat per process.

Connection supervision (`_connection_task_handler`; Luna owns the whole retry lifetime, not Pipecat):
- 10 s per connect attempt; 30 s total recovery budget, reset only after a connection that lasted ≥10 s.
  Backoff 0.5 s doubling to a 4 s cap. Codes 400/401/403/404/1007/1008 are fatal → back to wake listening.
- `GoAway` → `_renew_connection` waits for a safe turn boundary (or deadline − 1 s), then restarts.
- The resumption handle is reused for 2 h after a disconnect (Google's documented validity). A rejected
  handle → fresh session, seeded with ≤32,000 bytes of recent whole user turns, without triggering a reply.
- Pipecat's `_disconnect` clears `_completed_tool_calls` and `_tool_call_id_to_name`. Anything still needed must
  be copied into `_retired_calls` first. Results for retired call IDs are sent as user text, not as tool responses.
- Catalog changes (`integrations.catalog_revision`) force a new session, because Pipecat 1.12 drops `LLMSetToolsFrame`.
- Unsent audio while not ready goes into `_unsent_audio` (32 s cap → end the conversation).

## Gemini Live facts (as of Nov 2026)

- `gemini-3.8-live`: 131,072 input / 65,536 output tokens; audio-only responses (use output transcription for text).
  `thinking_level` is unsupported; affective dialog is removed and proactive audio is always on. Don't set those.
- Function calls default to NON_BLOCKING on 3.8. Pipecat declares synchronous tools `BLOCKING`, and tools with
  `cancel_on_interruption=False` `NON_BLOCKING`; their responses get `scheduling: WHEN_IDLE`.
- Typed input and retired results use `send_client_content(turn_complete=True)` + `send_realtime_input(text=" ")`.
  Client content alone did not trigger inference. **`turn_complete=true` interrupts any active generation.**
- Without compression a session is limited to 15 min of audio. Luna always enables sliding-window compression;
  with memory it triggers at 114,688 tokens with a 98,304 target. Connections last about 10 min; `GoAway` warns first.
- Schemas go through `parameters_json_schema` (full JSON Schema), not the OpenAPI subset. Never strip constraints
  to satisfy the model; execution validates against the original MCP schema.

## MCP runtime rules

- Config: `~/Library/Application Support/Luna/mcp.toml`, `[integrations.<id>]`. IDs match `[a-z][a-z0-9_-]{0,31}`.
  Transports: `url` (HTTPS, or HTTP on loopback), `command`/`args`/`cwd` (stdio), or `backend="workspace"`.
  Auth kinds: `none`, `mcp_oauth`, `google_api`, `spotify`, `api_token` (stdio only: `token_env` names the variable;
  the token lives in Keychain under `api-token:<id>` and goes only into that server's environment). Stdio `env = {}` adds fixed variables. Optional `tools = [...]` allowlist. Invalid entries are skipped.
- Published names are `<id>__<tool>`, or a sha256-derived name if longer than 64 characters or containing
  invalid characters. Control tools: `connect_integration`, `authorize_integration`, `read_tool_result`; Pipecat
  adds `cancel_authorize_integration`. Both modes add `get_weather`; voice mode adds `end_conversation`; memory adds `recall_memory`/`remember_memory`.
- Execution order: allowlist → auth state → connect if needed → canonical validation → per-integration
  `_operation_lock` → call → classify auth failure. Different integrations may run concurrently.
- **Never replay actions.** Login, reconnect and recovery must not repeat a completed or uncertain service call.
  Repeated call IDs reuse stored results.
- Results whose JSON exceeds 8,192 characters become a 4,096-character preview plus `result_id`, kept in RAM
  until shutdown and readable with `read_tool_result`.
- Failure payloads are `{"status", "integration", "reason"}` with safe reason codes. Never put provider responses,
  tokens or credentials in results or logs (`AuthFailure` messages are codes, not provider text).
- Keychain: generic password, service `Luna MCP` (`Luna MCP (dev)` from a checkout), account = sha256(server URL). Background access must never prompt;
  it returns `auth_required`/`keychain_access_required`. Only explicit authorization and setup may prompt.
- Authorization: loopback callback on `127.0.0.1:8765` (Spotify uses `:8888`), PKCE, 295 s deadline inside
  Pipecat's 300 s tool timeout. One final async result; Gemini Live can't receive progress updates.
- Canvas (Quercus, `canvas-mcp` 1.18.0 in its own Python 3.13 venv under `servers/canvas-mcp`): student role,
  `ALLOWED_WRITE_TOOLS=none` (unset exposes every write tool over stdio), code execution off, read tools allowlisted.
  Two levels: Luna's voice/notch agent gets the allowlisted read tools (`setup_canvas.TOOLS`; `--update` refreshes an
  existing entry), and the dashboard syncs deterministically (tasks from assignments/quizzes with submission state,
  announcements) by parsing canvas-mcp's text output (`call_tool(bounded=False)`). Parsers are pinned to 1.18.0's
  formats; check `tests/test_canvas.py` samples before upgrading. Never add Canvas write tools.
- Spotify `playMusic` runs only with identifiers returned by Spotify in this session or given by the user
  (URI, URL or ID); otherwise it returns `unresolved_playback_identifier`. Device IDs never count.
- Workspace: no send or permanent-delete in Gmail; Calendar writes use ETags; Sheets rejects `writeControl`;
  downloads capped at 32 MiB; writes are never retried and partial failures report `completed_requests`.
  Credentials keep the hosted URLs as Keychain keys (`credential_resource`) so both backends share grants.
- Bundled sign-in apps hold no real secret: Google takes only a Desktop app client (`installed` JSON; public by Google's
  design, PKCE, redirect `http://127.0.0.1:8765/`), and Spotify signs in with PKCE and refreshes with the client ID alone
  (`spotify-pkce.patch`; `spotify-config.json` has no `clientSecret`). Never reintroduce a Web client or a Spotify secret.
  Upgrades: app setup (`configure(replace=True)`) swaps in a different shipped Google client and drops its tokens; Spotify
  replaces a server whose `luna-patches.sha256` differs and drops a config with `clientSecret` (both then read as not ready).

## Audio, wake word, UI

- Two AVAudioEngines: normal playback, plus a capture engine with voice processing enabled and its output kept
  silent (macOS uses system playback as the echo reference). AGC off, ducking at minimum. Don't merge them:
  routing speech through the voice-processing output audibly degrades it (see the gitignored
  `diagnostics/audio/20261002-voice-path-probe/findings.md`).
- Interruption clears only playback (`_clear_playback`) and keeps engines running. The capture watchdog
  (1 s tick; 5 s without callbacks, a sleep gap, or a stopped engine) pushes a permanent error → pipeline rebuild.
- Wake model `sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` (chunk-8 fp32, sha256-pinned) lives in
  `~/Library/Application Support/Luna/models/`. Phrase phones come from `en.phone`; threshold 0.25.
- Touch AppKit only on the main thread. Agent → UI events go through a `queue.Queue`: `bool` = expand/collapse,
  `(speaker, levels|None, t)` = audio levels, `None` = quit. Audio buffers never cross threads, only levels.
- The notch text chat is wired to `TextAgent`; history (with reply status and model) lives in `text-chats.sqlite3`.
  Attached files are only named to the model, never read. Hold-to-talk is not implemented.

## Dashboard

- Runs inside Luna (`src/ui/app.py`, built hidden: `DashboardApp.build(show=False)`); `--dashboard` runs it alone for
  development. Quitting or failing takes Luna down with it, and the other way round. No agent or memory calls. Its one MCP use is the Quercus keeper
  (`canvas.py` `Quercus`, launch + every 15 min, a fresh canvas-mcp process per sync; `quercus=None` in demo and tests).
  Its one model call is `picks.py`:
  a Gemini Live session once a day (never Flash: that quota is the notch chat's). Details in `docs/dashboard.md`.
- The server binds 127.0.0.1 on a random port; every request needs the per-launch token cookie, a loopback Host,
  and same-origin writes. Files are served only from project folders/items and `~/Library/Messages/Attachments`.
- Never write to `~/Library/Messages/chat.db`. Sending uses `osascript` with text and chat ID as argv. The Beeper token
  lives in Keychain (`Luna Dashboard`/`beeper`); provider failures surface as reason codes, never provider text.
- The window is an ordinary one: one native HUD blur fills it (tinted per Settings), normal level, Luna has a Dock icon
  (regular activation policy; the notch-only `--preview` stays an accessory). A global shortcut (`hotkey.py`, Carbon
  `RegisterEventHotKey`, no Accessibility needed; default ⌃⌥Space) and the Dock icon bring it to the front; it never
  hides on deactivation (permission prompts would send it away), only on close/⌘W. `place_lights` centres the traffic
  lights on the page's top bar (rerun on resize and after full screen). Panels are CSS glass (`data-pane` + `data-glass`);
  sheets and menus use `backdrop-filter` over page content. Panes in sized rows must not size from content
  (absolute `.pane-body`, inner `.pane-scroll`). PyObjC blocks that return a value abort the app: completion
  handlers must return None.
- Visual rules: `docs/dashboard.md` → Design language (tokens in `web/css/glass.css`). Five violet-tinted ink steps; lilac
  primary action; violet focus and in progress; corona only for now/next and Luna's picks for today; coral
  overdue/problems; amber needs-your-reply; mint done; eight identity hues for projects and courses (via `--h`). One type
  scale (28/20/15/13.5/12.5/11.5) and one spacing scale (4/8/12/16/24/32/48). Open fields on hairlines; corners 14/10/8/6.
  No filler copy. Don't add colours outside the palette, glows or text shadows.
- Calendar: Luna's own events live in the `events` table (important flag, guests, RSVP); Home merges them with
  `calendar.py` (EventKit, read-only). Reading EventKit never prompts; only `POST /api/calendar/access` (a user click)
  may show macOS's prompt, and only when running from `build/Luna.app` (a shell-started Python is refused silently).
  Never write to EventKit. Google Calendar sync is out of scope: Luna is meant to replace it.
- Model (schema v9; v8 adds `tasks.external_id`, v9 `tasks.external_state` and per-course `announcements`, both from Quercus): projects (category, own colour/icon optional, archived, no dates; progress = done/all tasks, repeating ones excluded),
  courses (code, colour, icon, active), tasks (status todo/doing/done, Markdown description, one owner or none, no
  estimate), notes (Markdown files), events. Archiving a project or finishing a course ends its tasks' recurrence.
- Notes are `.md` files in `<folder>/Notes/` (media in `Notes/assets/`), else under the data folder; Luna writes only
  there inside a user's folder, renames with the title, and trashes (never unlinks) on delete. Media is served only
  from inside an owner's root (`notes.resolve`).
- Weather: `location.py` (CoreLocation on the main thread; never prompts except `POST /api/location/access`, a user click,
  and only from `build/Luna.app`) + `weather.py` (Open-Meteo; with `picks.py`, the dashboard's only internet calls). Coordinates leave rounded
  to 1 decimal; never log them or provider text. Tests fake both; never contact Open-Meteo or Gemini in default tests.
- `get_weather` (notch process) reads only `weather_place` and `units` from the dashboard DB, read-only; with no saved place it
  returns `location_unavailable` (the notch app never asks for location). Named places go to Open-Meteo's geocoder.
- Picks: event titles never leave the Mac (only times); task text is untrusted data in the prompt. Due today or
  tomorrow is always a pick, decided locally; the model only adds up to three.
- Front end: plain modules, no CDN. The one bundle is the editor (`cd src/dashboard/editor && npm install && npm run build`;
  output and KaTeX fonts are committed, `node_modules` is not); it loads lazily. Re-render through `core.js` `morph`;
  the editor host uses `data-keep` so re-renders never touch it. The editor has Read and Edit modes, switched only by ⌘E or the bar button (new or empty docs open in Edit, others in Read);
  never make a decoration depend on the cursor or focus, so nothing moves within a mode. Editor block widgets must not have vertical CSS margins
  (CodeMirror measures without them, so lines below drift): space them with padding on the `.md-block` wrapper. Page changes go through `transition()` in `core.js` (View
  Transitions; a 1.5 s fallback because a hidden window never paints). Use the motion tokens (`--ease-*`, `--t-*`), and
  never put a live `filter`/`backdrop-filter` on anything full-window: it makes transition captures slow enough to skip; background `load()`s wait while a
  navigation is loading so they can't cancel it. The optional backdrop is a picture folder from Settings, served only
  from inside that folder. Top bar: Home, Projects, Academia, Settings (plus `#/setup`, outside the bar, for first run);
  other domains keep their APIs until the side navigation exists.
- Tests: `tests/test_dashboard.py` (fake `chat.db`, fake Beeper server, temp git repo, aiohttp test client).

## Memory

- Endpoints: Hindsight on `127.0.0.1:8889` (bank `luna`), Postgres (pg0) on `127.0.0.1:55439`, Ollama on
  `127.0.0.1:11435` (`qwen3.8:27b-q4_K_M`, 32k context, `OLLAMA_KEEP_ALIVE=1m` so the ~18 GB model unloads when idle).
  LaunchAgents: `dev.luna.memory`, `dev.luna.memory-ollama` (both `KeepAlive`; stop with `launchctl bootout`, not kill).
  A separate `LUNA_HOME` uses offset ports and `.<profile>` labels (`src/paths.py`), sharing only `~/.ollama`'s model files.
- `Memory.open()` yields `None` when not set up; every caller must handle that. Data lives in `~/Library/Application Support/Luna/memory/`.
- Each `record()` inserts the turn and a full conversation snapshot into `luna_memory.outbox` in one transaction.
  `_drain` sends one snapshot at a time under advisory lock 1280659009 and only marks it delivered after the
  Hindsight operation completes. An existing `operation_id` keeps it on retry; failure gets a new one.
- Startup context: five mental models (profile, preferences, projects, people, recent) plus 8 recent turns,
  capped at 96,000 bytes, placed in the system instruction so compression can't remove it.
- Memory and tool output are untrusted data. Prompts must keep saying so; never treat them as instructions or authorization.

## Conventions

- Style: compact modules, a one-line module docstring, comments only for non-obvious *why*, `loguru` logger,
  sparse type hints, lines up to about 120 characters. Match the surrounding code rather than reformatting it.
- Tests: `unittest` (`IsolatedAsyncioTestCase`). Reuse `FakeGemini`/`FakeSession` from `tests/test_mcp_pipeline.py`
  (fault variants in `tests/test_session_recovery.py`). Exercise the real Pipecat connector against the fake server.
  Never contact real Google, Spotify or Gemini in default tests; live tests stay behind `LUNA_LIVE_TESTS=1`.
- Behavior changes need a regression test, especially in recovery, tool ordering, auth and no-replay paths.
- Secrets: `.env` (`GOOGLE_API_KEY`, `SPOTIFY_*`) is gitignored. Never print, log or commit it, and never read
  Keychain items or `spotify-config.json` contents. `diagnostics/` (gitignored) holds microphone recordings.
- Git: short prefixed subjects (`chore: …`). Write commits and PR text in the maintainer's own voice with no AI
  attribution lines. Commit only when asked.
- Docs: update `README.md` and the matching `docs/*.md` with any behavior change. October docs
  (`fast-tools-implementation`, `three-strategy-benchmark`, `tool-strategies-benchmark`, `tool-directory`) are
  historical and marked as such. Dated measurements and test counts are snapshots; don't "update" them without re-running.

## Known risks and open items

- Retired tool results (after a forced new session) are sent with `turn_complete=True`, which can cut off
  speech in progress. This comes from Google's docs and hasn't been observed yet.
- First-speech latency often exceeds 2 s (`docs/gemini-live.md`); there is no acknowledgement or replay workaround, by design.
- Large tool results stay in RAM for the whole session; there is no disk spill (see the `shortcut:` comment in `mcp.py`).
- Wakes behave differently depending on memory: without it, context builds up across wakes and up to 32 KB is
  reseeded each time (`test_end_conversation_disconnects_and_allows_repeated_wakes` asserts this); with it, each
  wake starts clean. Keep both paths in mind when changing `_load_memory` or `_create_initial_response`.
- Hindsight 1.2 is current upstream; Luna pins 1.1.0.

## References

Google: [Gemini 3.8 Live](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live),
[session management](https://ai.google.dev/gemini-api/docs/live-api/session-management),
[Live tools](https://ai.google.dev/gemini-api/docs/live-api/tools). Pipecat:
[Gemini Live service](https://docs.pipecat.ai/api-reference/server/services/s2s/gemini-live),
[PipelineWorker](https://docs.pipecat.ai/api-reference/server/pipeline/pipeline-worker). MCP:
[2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog),
[Python SDK](https://py.sdk.modelcontextprotocol.io/). Others:
[sherpa-onnx KWS](https://k2-fsa.github.io/sherpa/onnx/kws/index.html),
[Hindsight](https://hindsight.vectorize.io/), [Ollama FAQ](https://docs.ollama.com/faq).
Prefer reading the installed package source in `.venv` over memory or web docs when they disagree.
