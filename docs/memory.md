# Persistent personal memory

Luna uses the pattern demonstrated by the community
[Hindsight LiveKit integration](https://github.com/namandidthis/hindsight-livekit):
startup context, a recall tool, and retention of finalized conversations. It uses
Luna's existing Pipecat Gemini Live connection directly; LiveKit is not required.
Live audio reaches Gemini before a finalized text transcript exists, so retrieval
is not automatically inserted ahead of every answer. The prompt directs Luna to
recall missing, ambiguous, conflicting, or old personal details, including indirect
references, and to search before claiming a topic was never discussed.

## Lifecycle

1. Setup installs a separate Python 3.13 Hindsight 1.1.0 environment. Luna's own
   environment only adds the matching client and asyncpg. This avoids mixing the
   service's inference dependencies into the voice pipeline.
2. macOS LaunchAgents run a dedicated native Ollama server on `127.0.0.1:11435`
   (ports, labels and the Postgres name below are your own Luna's; a separate `LUNA_HOME`
   gets its own, from `src/paths.py`, and shares only Ollama's model files)
   and Hindsight on `127.0.0.1:8889`. The Hindsight agent runs
   `scripts/memory_service.py`, which starts a dedicated pg0-managed PostgreSQL
   instance on `127.0.0.1:55439` before launching Hindsight. Hindsight's LLM calls
   go to that Ollama server's OpenAI-compatible `/v1` endpoint; embeddings and
   reranking use local ONNX models. None binds to the network interface.
   `OLLAMA_KEEP_ALIVE=1m` unloads the model (about 18 GB) one minute after the
   last request, so RAM is held only while extraction or note refreshes run;
   Hindsight sends its requests back to back, so a batch loads the model once.
   Setup writes this into the LaunchAgent; re-run `setup_memory` (or add the key
   to the installed plist) for an existing install.
3. Each app runtime has a session UUID. Every wake has a new conversation UUID.
   Connection resumption, tool-catalog refresh, and socket renewal preserve that
   conversation. A new wake clears its transient Gemini history and reloads notes.
   The typed Live window has one conversation per window runtime. Each island text
   chat uses its chat ID as its conversation UUID, so it continues across restarts.
4. Pipecat's `on_user_turn_message_added` saves finalized voice text; typed input
   is saved before forwarding. Island chats save the user turn before generation
   and the reply only once it completes. `on_assistant_turn_stopped` saves assistant text,
   marking interrupted output as possibly unheard. These handlers finish during
   pipeline cleanup. There is no raw audio archive.
5. A database transaction appends the source turn and a pending conversation
   snapshot together. The outbox worker coalesces unsent snapshots and retains
   them under a stable Hindsight document ID. A PostgreSQL advisory lock serializes
   delivery across app processes. An in-flight snapshot is resumed before a newer
   replacement; uncertain acknowledgements retry the same operation UUID. Failed
   operations get a fresh UUID. Only completed extraction marks work delivered.
   Delivered snapshots shed their duplicate text; original turns remain intact.
6. Hindsight extracts detailed facts, links entities, and consolidates observations
   locally. Five stored mental models maintain profile, preferences, projects,
   people, and recent discussions. They refresh after consolidation, with a
   five-minute minimum interval. Source timestamps, corrections, uncertainty, and
   reasons behind decisions are part of the extraction instructions.

Pending work survives app termination. Hindsight can finish already submitted
work while Luna is closed; unsent work resumes when Luna next opens. A database
failure during capture stops the affected pipeline visibly. API or extraction
failures leave the durable outbox pending and retry. Failed topic-note refreshes
are explicitly retried after five minutes; Hindsight otherwise pauses automatic
refresh after a failure. Recall failures are reported
as unavailable memory rather than an empty archive.

## Startup context and recall

Topic notes plus the latest eight bounded transcript turns are loaded before
Gemini connects. Notes include refresh timestamps, stale/failure flags, and a
pending-extraction count. The JSON payload is bounded to **96,000 UTF-8 bytes**;
large notes are shortened while preserving every topic. This is approximately
24,000 English tokens, not an exact Gemini token count. Hindsight's note targets
total 26,000 tokens; all original sources remain available through recall.

Gemini 3.8 Live has a **131,072-token input limit**, rather than the million-token
window of some non-Live models. The existing full tool catalog uses roughly
53,000 prompt tokens. Memory-enabled connections request sliding-window
compression at 114,688 tokens with a 98,304-token target, leaving space for tools,
startup notes, and ongoing audio. Startup notes are in the setup system instruction
so they survive conversation-history compression. The byte limit is conservative
for ordinary English; unusual text and a larger tool inventory can require a
smaller memory budget.

The local memory model has a separate 32,768-token context. Extraction and note
generation use bounded output and a 20,000-token reflection context, with thinking
disabled. Background note refresh can take several minutes on this Mac; it does
not hold up the live voice connection.

`recall_memory(query)` returns dated facts, source excerpts, and bounded recent
user statements. It uses Hindsight's local semantic, keyword, temporal, and graph
retrieval plus ONNX reranking. `remember_memory(text)` durably records an explicit
request or correction, clearly labeled as Luna's paraphrase. Saving succeeds
before extraction finishes. Neither tool can reset the bank.

The prompt treats memory as evidence, never instructions or authorization.
Current explicit corrections take priority. Assistant suggestions and interrupted
speech are not reliable statements about the user or proof that an action ran.
These are model instructions, not a guarantee that every derived fact is correct;
source transcripts remain available for checking exact wording.

## Local storage and reset

`~/Library/Application Support/Luna/memory/` contains:

| Path | Contents |
| --- | --- |
| `postgres/` | Source turns, pending work, Hindsight facts, documents, observations, notes, and operations |
| `models/` | Local ONNX embedding/reranking models and tokenizer cache |
| `runtime/` | Isolated Hindsight Python environment |
| `config.json` | Setup version and bank identity, without integration secrets |
| `dev.luna.memory*.log` | Service diagnostics |
| `access.lock` | Shared app / exclusive setup-and-reset lock |

Ollama weights remain in its normal model store. Memory uses the fixed bank
`luna` and the dedicated PostgreSQL cluster; it does not use the integration MCP
servers for storage. The services trust local processes and are not configured
for remote or multi-user access. Source transcripts are ordinary local database
data, not a separately encrypted vault. Normal Mac disk encryption and backups
remain relevant. Historical transcripts may contain anything spoken; the model's
instruction to exclude secrets from derived facts does not redact source text.

```sh
uv run python -m scripts.setup_memory
uv run python -m scripts.reset_memory --yes
```

Reset requires all Luna memory runtimes to be stopped. It stops both services, checks
that pg0 owns the expected directory, drops the entire dedicated cluster, clears
both service logs, restarts the services, and initializes an empty schema/bank/notes.
Dropping the cluster removes database history and pending extraction too, so old
test facts cannot reappear from an ingestion queue. An interrupted reset can be
rerun. The script preserves models, other Luna data, Google/Spotify configuration,
and Keychain credentials. External backups and terminal logs are outside its scope.

## Verification

```sh
uv run python -m unittest discover -s tests -q
```

The memory tests exercise startup context through the real Pipecat connector and
a simulated Gemini Live server, recall-tool results, finalized voice/typed
capture, interruption metadata, byte bounds, and reset exclusion/path checks.
For durable outbox tests, create a disposable PostgreSQL database whose name ends
in `_tests`, then run:

```sh
LUNA_MEMORY_TEST_DATABASE_URL=postgresql://hindsight:hindsight@127.0.0.1:55439/luna_memory_tests \
  uv run python -m unittest tests.test_memory -q
```

These tests truncate that database's `luna_memory` tables. They verify duplicate
capture, reopening, per-wake identity, stable operation IDs after a lost
acknowledgement, snapshot coalescing, failed extraction, and completion checks.
They use a simulated Hindsight client and do not perform slow model extraction.

On this Mac, the full suite passed 147 tests with one unrelated skip and the
database tests enabled. A real local extraction preserved a synthetic decision
and its reason in 36 seconds. Reopening the runtime and recording a correction
made the new choice recallable. The generated project notes preserved both
choices and their reasons, and reached the next startup context. A real Gemini
Live response called `recall_memory` and answered with the corrected choice in
12.49 seconds after connection; no microphone or speaker was used for this check.
Local document generation also exercised Hindsight's malformed-output retry and
repair behavior, so note refresh is slower and less predictable than retrieval.

The reset command was exercised against that data. Source turns, outbox rows,
facts, documents, and the disposable test database were gone afterwards; five
empty note definitions remained. The synthetic facts no longer recalled, and
model availability plus integration-file fingerprints were unchanged. The store
was left empty for normal use.

References: [Hindsight](https://github.com/vectorize-io/hindsight),
[LiveKit example](https://github.com/namandidthis/hindsight-livekit/blob/main/examples/realtime_agent.py),
[Gemini Live model](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live).
