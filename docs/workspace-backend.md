# Local Workspace MCP backend

Luna now defaults to a local Workspace MCP server using public Google APIs. This
avoids the hosted MCP enrollment restriction verified for this project's Gmail
server. Public API access still depends on enabled APIs, OAuth scopes, account
policies, and each product's availability.

## Ownership and flow

```text
Gemini Live ↔ Pipecat functions ↔ MCPRuntime ↔ native MCP server ↔ Google REST APIs
                                           ↔ existing OAuth / Keychain records
```

The MCP SDK supports an in-process Server as a Client transport. Luna uses that
native facility rather than running eight child processes. `MCPRuntime.from_config`
selects the server and auth adapter; callers, Gemini publication, and both voice
and typed pipelines are unchanged. The backend has one persistent HTTP client per
service. The parent runtime and backend share an auth adapter and credential cache.
The same server can also run over standard MCP stdio:

```sh
uv run python -m src.integrations.workspace --service gmail
```

`src/integrations/workspace/tools.json` contains the 59 recorded declarations,
including their original descriptions, schemas, annotations, and output contracts.
Tests compare them against the original snapshot. The service modules implement
those functions only; there is no Workspace allowlist or denylist to maintain.
Other MCP integrations retain their existing optional allowlists.

| Module | Responsibility |
| --- | --- |
| `server.py` | Catalog, canonical input validation, native server lifecycle, explicit errors |
| `auth.py` | Google OAuth Flow with PKCE, existing Keychain storage, synchronized refresh |
| `client.py` | Fixed Google origins, authenticated requests, bounded downloads, write receipts |
| `gmail.py`, `drive.py`, `documents.py`, `calendar.py`, `chat.py`, `people.py` | Public API calls and contract normalization |

The existing runtime still owns per-integration ordering, authorization deduplication,
cancellation, and retained-result paging. The server independently validates
arguments, including when used over stdio. Successful writes followed by a later
failure return completed-request receipts. Timeouts and server failures during a
write mark its outcome unknown; neither the API client nor runtime retries that
action automatically. Calendar changes use the event's ETag. Gmail offers drafts,
labels, spam, and trash operations, with no send or permanent-delete endpoint.

Background access never prompts for Keychain permission. Explicit authorization or
setup can prompt. The local adapter uses the same service-bound records as the
hosted adapter, retains existing refresh tokens, and requests additional write
scopes only from the service's reviewed scope set. A normal tool call never opens
a browser. No additional Google scopes were granted during this verification.

## Switching and rollback

```sh
uv run python -m scripts.setup_google --backend local
uv run python -m scripts.setup_google --backend hosted
```

Restart Luna afterwards. The script changes only recognized Workspace tables,
preserves unrelated integrations and credential records, validates the resulting
TOML, saves a timestamped backup next to `mcp.toml`, and replaces the file atomically.
Repeated selection is a no-op. Hosted access continues to depend on Google's
preview or GA access rules; switching cannot override Google project restrictions.

The installed configuration was switched to local on November 2, 2026. All eight
Workspace catalogs discovered (59 declarations) in the configured runtime. Spotify
still discovers 30 upstream declarations and publishes its existing six allowed
tools. The complete Gemini setup therefore remains **69 tools / 202,949 bytes**.

## Public API differences

The declarations remain compatible. Some hosted behavior has no exact public API
equivalent, so this is not a claim of perfect semantic parity:

- Calendar `search_events` uses the public API's keyword query, not hosted natural
  language interpretation. Gemini should express dates and ordering with `list_events`.
- Drive metadata does not manufacture generated content snippets. Text reads cover
  native Docs, Sheets, Slides, text formats, PDF text, and supported Office/OpenDocument
  XML. Image OCR and scanned-PDF OCR are unavailable. Unsupported text conversion
  explicitly returns `textFormattingNotSupported`; an empty extraction is not proof
  that an image or scanned document has no content.
- Exporting a specific revision of a native Google file fails explicitly; it never
  exports the current revision as if it were the requested older revision.
- The public Sheets API does not support nonempty `writeControl`. Such a request
  fails before mutation rather than silently dropping concurrency protection. Docs
  and Slides forward their supported write controls.
- Chat name matching scans a page of joined spaces to preserve substring matching.
  Participant searches check memberships in joined group chats because Google's
  `findGroupChats` matches an exact roster and would omit larger groups. This can
  require extra requests; follow continuation tokens even after empty filtered pages.
  Public message search also has Google's documented exclusions for certain message
  types, blocked users, and muted spaces.
- Binary downloads and expanded Office archives are limited to 32 MiB. Oversized
  downloads fail explicitly; they never return a successful truncated file.

## Verification

```sh
uv run python -m unittest discover -s tests -q
uv run python -m scripts.benchmark_tools --backend recorded --audible --repeats 2 \
  --cases greeting,gmail,calendar,docs,large_result,playback,history_playback,multi_service,unsupported \
  --timeout 45 --output /tmp/luna-workspace-recorded.json
uv run python -m scripts.benchmark_tools --backend local --audible --repeats 2 \
  --cases greeting,gmail,calendar,docs,large_result,playback,history_playback,multi_service,unsupported \
  --timeout 45 --output /tmp/luna-workspace-local.json
```

The suite ran **81 tests: 80 passed, one opt-in live test skipped**. This includes
all 59 tools through the actual MCP SDK, validation against original output schemas,
canonical input rejection, authorization state and PKCE, denial and cancellation,
refresh/revocation, backend rollback, native/stdio catalogs, calendar DST and timezone
handling, ETags, MIME encodings, pagination, binary limits, partial writes, and no
automatic replay. This is representative parameter coverage, not every API option.

Read-only checks against **real Gmail**, using the configured runtime and existing
grant, completed label reads in **0.40 / 0.24 / 0.21 seconds** and a one-thread inbox
metadata search in **0.58 seconds**. An earlier first label read took 1.14 seconds.
Reports contain only counts and timing. No real data was modified. A background
Keychain denial is also retained as evidence; the explicit check succeeded after OS
approval. Other products and real mutations were tested with synthetic API responses,
not live account writes.

## Latency results

These are contemporaneous runs on the M5 Pro using real `gemini-3.8-live`, full
schemas, typed input, and native Mac speaker output. The baseline uses recorded
MCP responses over local HTTP. The local variant runs the production Workspace
server and API adapters against synthetic REST responses with a 5 ms delay per
request. Spotify remains synthetic in both. All effects are synthetic; the separate
Gmail read above measures real public API latency.

| Measurement | Recorded MCP baseline | Local backend |
| --- | ---: | ---: |
| Correct tasks | 18/18 | 18/18 |
| First speaker render, median | 2.11 s | 2.03 s |
| First speaker render, empirical p95 | 6.01 s | 5.01 s |
| Two-second speaker misses | 10/18 | 10/18 |
| Final tool result, median (14 tool turns) | 1.49 s | 1.54 s |
| Final tool result, empirical p95 | 5.40 s | 4.39 s |
| Final tool result below ten seconds | 14/14 | 14/14 |
| Spoken reply complete, median | 5.97 s | 6.35 s |
| Spoken reply complete, empirical p95 | 13.29 s | 16.31 s |
| Spoken reply complete below ten seconds | 13/18 | 14/18 |

| Task | Local first speaker render, median | Local service sequence complete, median |
| --- | ---: | ---: |
| Greeting | 0.86 s | — |
| Gmail labels | 1.78 s | 0.63 s |
| Ordered calendar list | 1.94 s | 0.86 s |
| Exact document replacement plus verification | 4.83 s | 4.19 s |
| Large document and retained-result retrieval | 2.25 s | 0.78 s |
| Correct Spotify recording | 2.18 s | 1.53 s |
| Recording referenced from history | 3.21 s | 1.77 s |
| Calendar and Spotify | 2.33 s | 1.36 s |
| Unsupported action refusal | 1.41 s | — |

Service sequence completion measures the required external calls; the large-result
row excludes later internal page retrieval. Final tool-result timing above includes
all observed tool results. Spoken completion includes the time needed to say the
answer. Speaker timing uses the native render callback, not acoustic measurement
or human judgement of usefulness. All speech comes from Gemini; no canned response
or inference gate was added.

Each trial opened a fresh Live session before submitting the request. Runs were
sequential, baseline first, with two repetitions per case, not randomized. The small
sample does not establish percentile guarantees, a 100-turn promotion gate, or
production latency across all Google services. The observed tool sequences meet
the ten-second objective; consistent two-second speech remains unmet. The higher
local spoken p95 comes from the multi-service answer; the service sequence itself
completed in under two seconds. The samples do not demonstrate a statistically
significant speed improvement or regression.

Raw results, the failed middleware smoke run, sanitized Gmail reports, environment
versions, source hashes, and test output are in [`benchmarks/workspace/`](benchmarks/workspace/).
The failed smoke advertised only ten tools because the benchmark middleware omitted
its continuation's context argument; it is not counted as a successful trial.

Public API contracts were checked against Google's discovery documents for
[Gmail](https://developers.google.com/workspace/gmail/api/reference/rest),
[Calendar](https://developers.google.com/workspace/calendar/api/v3/reference),
[Chat](https://developers.google.com/workspace/chat/api/reference/rest),
[Drive](https://developers.google.com/workspace/drive/api/reference/rest/v3),
[Docs](https://developers.google.com/workspace/docs/api/reference/rest),
[Sheets](https://developers.google.com/workspace/sheets/api/reference/rest),
[Slides](https://developers.google.com/workspace/slides/api/reference/rest), and
[People](https://developers.google.com/people/api/rest).
