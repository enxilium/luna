# Spotify MCP return compatibility

The pinned Spotify MCP server registers 30 tools; Luna's configured allowlist still
publishes six. Their results are text or Markdown, not structured JSON. Previously,
Luna only recorded identifiers from JSON. A valid ID returned by `getMyPlaylists`
therefore failed Luna's own playback guard before Spotify received a request.

The shared result handling now lives in `src/integrations/spotify.py`. It preserves
the original content and handles both the pinned text formats and JSON/structured
responses. Canonical argument validation and the configured allowlist still apply.

| Exposed tool | Returned format | Handling |
| --- | --- | --- |
| `searchSpotify` | Numbered results with trailing `- ID:`; empty-result notice; text error | Associate IDs with the requested media type; recognize errors |
| `getMyPlaylists` | Numbered playlist IDs; first/last row and total in heading; empty-page notices | Remember playlist IDs before context previews; preserve pagination information |
| `getNowPlaying` | Markdown `**ID**:` plus playback/device details; paused/nothing/podcast notices | Remember the track ID when available; preserve all other fields |
| `getAvailableDevices` | Multiline device IDs and state; no-device notice | Preserve data without treating device IDs as music IDs |
| `playMusic` | `Now playing: spotify:...` or a textual error | Only report success after checking playback state; errors remain failures |
| `pausePlayback` | `Playback paused` or a standard MCP error | Preserve the receipt and failure state |

Playback accepts all three documented forms: `uri`, `context_uri`, and `type` plus
`id`. Returned music IDs are retained as canonical typed URIs. An album/playlist ID
cannot be reused as a track ID. Explicit user URIs retain their type too. Failed
results, device IDs, arbitrary IDs inside names/descriptions, and placeholder IDs
do not establish playback identity. Podcast/show search output remains available,
but Luna does not infer support for episode playback from a track/context-only tool.

The guard runs inside the integration's execution lock. A lookup completing ahead
of playback in the same batch records its results before playback validation.
Nothing replays an action automatically. An unresolved ID now explicitly reports
that Luna blocked the call and Spotify playback was not attempted.

## Playback confirmation

The original pinned `playMusic` returned `Now playing` after sending a command,
without reading playback state. Its SDK wrapper also suppressed selected parsing
errors, allowing a malformed response to become a success receipt. The receipt
alone could therefore accompany silent or unchanged playback.

`scripts/patches/spotify-playback.patch` fixes this in the Spotify server. Playback
uses its existing direct HTTP helper so HTTP and parsing errors reach Luna. After
one play command, the tool reads playback state up to four times, waiting 500 ms
between reads. Success requires Spotify to report playback on the selected device
with the requested track (including relinked tracks), or the requested album,
artist, or playlist context. A reported volume of zero returns an explicit error.
Unconfirmed playback also returns an error explaining that the command was
accepted and current playback should be checked before retrying. The tool never
repeats the play command to verify it.

Direct requests using `uris: [track]` were also reproduced against the real
desktop player: Spotify returned HTTP 204 while playback remained stopped with
`item: null`, including a read more than nine seconds later. The device was
active, unrestricted, and at 100% volume. A request for the same recording using
its album as `context_uri` and the track URI as `offset.uri` started playback,
with the correct identity and advancing progress. This matches the
[reported Spotify regression](https://community.spotify.com/forum/en/discussion/comment/7556803).

The patch therefore resolves a track's album before sending its one play command
and uses that album with an exact track offset, as supported by the
[Spotify playback API](https://developer.spotify.com/documentation/web-api/reference/start-a-users-playback).
It still verifies the requested track, not just the album. Missing or invalid
album metadata fails before any playback attempt. Album, artist, and playlist
requests keep their existing context and positional offset. This workaround can
continue playing subsequent album tracks after the requested song ends.

Spotify setup applies the patch before building, accepts an already patched
checkout, and refuses conflicting changes without overwriting them. Re-run
`uv run python -m scripts.setup_spotify` to apply it to another installation, then
restart Luna. This confirms Spotify's reported state; it cannot prove sound
reached the speakers.

## Verification

`scripts/fixtures/spotify-results.json` records **180 real MCP response envelopes**
from all 30 tools, including successful and failing calls under legacy and
`2026-07-28` protocols, plus search media types, playing/paused/podcast states,
empty pages, missing playback arguments, and unflagged volume failures. These were captured through the
upstream SDK using mocked HTTP and in-memory credentials. No real mutations ran.
Playback cases additionally cover accepted commands without playback, paused or
incorrect playback, wrong contexts/devices, mute, delayed state updates, relinking,
all context types, play-request errors, and missing/invalid/failed album lookups.
Expected HTTP sequences enforce the album lookup and exact track offset, one
play command per attempt.
The fixture includes upstream source hashes. Regenerate it with Luna's installed
Node runtime:

```sh
"$HOME/Library/Application Support/Luna/runtime/node-v26.8.1-darwin-arm64/bin/node" \
  scripts/fixtures/capture_spotify_formats.mjs
uv run python -m unittest tests.test_spotify_results tests.test_mcp_tools tests.test_integrations -q
uv run python -m unittest discover -s tests -q
```

Playback validation passed all 180 captures, 62 upstream Spotify tool
tests, and Luna's full suite (144 passed, nine skipped). The full upstream suite
passed 91 of 92 tests; its unrelated `127.0.0.2` bind test failed with
`EADDRNOTAVAIL` on this Mac. The saved patch also applied twice to pristine pinned
sources and produced the installed files unchanged. Real Spotify playback was
verified using both direct API requests and Luna's search/play/current-playback
tools. Speaker output was not independently measured.

The earlier compatibility audit ran **89 tests: 88 passed, one opt-in live test skipped**.
Regression checks cover the captured envelopes, original content preservation,
JSON and structured alternatives, all playback aliases, wrong-type/unknown-ID
rejection, errors without flags, device-ID separation, explicit user references,
batch ordering, and full ID retention before oversized-result paging.

In the earlier compatibility audit, four **real read-only calls** succeeded through the configured Spotify MCP:
playlists **0.35 s**, search **0.62 s**, current playback **0.29 s**, and devices
**0.12 s**. All three playback argument forms passed Luna's guard using the real
"the hermit" playlist ID, with the downstream playback call stubbed. Wrong-type
playback remained blocked. Reports retain counts and timings, not private titles,
device names, or identifiers. Actual playback and pause were not triggered.

The benchmark now uses the pinned server's text/Markdown instead of fabricated
JSON Spotify responses, and includes a named-playlist case:

```sh
uv run python -m scripts.benchmark_tools --backend local --audible --repeats 2 \
  --cases playlist,playback,history_playback,spotify --timeout 45 \
  --output /tmp/luna-spotify-formats.json
```

With real Gemini Live and synthetic service effects, **8/8 trials passed**. Median
service-sequence completion was **1.25 s** for playlists, **1.45 s** for searched
recordings, **2.12 s** for a recording referenced from history, and **0.73 s** for
current playback. First speaker render medians were **2.20 / 2.18 / 3.35 / 1.52 s**,
respectively. These small sequential samples establish no percentile guarantee;
consistent two-second speech remains unmet.

Results and test logs are in [`benchmarks/spotify-formats/`](benchmarks/spotify-formats/).
Earlier benchmark JSON Spotify fixtures did not exercise the real return format;
their Spotify compatibility claims are superseded by these checks. They remain
historical timing evidence for those synthetic fixtures.
