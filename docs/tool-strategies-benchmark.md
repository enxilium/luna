> Historical October 2026 investigation. Luna now uses Gemini Live; see [the current migration](gemini-live.md). Old commands, source paths and timing claims below refer to the superseded local-model implementation.

# Tool strategy A/B

Historical comparison. Luna now uses selective publication exclusively; directory
and executor implementations and strategy switches have been removed. Commands below
describe the measured checkout and are not supported by the current runner.

Measured October 2, 2026 on this Mac with local Ollama/Qwen, VoiceStudio sentence TTS,
Pipecat 1.12.0 and MCP SDK 2.1.1. Both strategies remain available; directory is the
default requested architecture. These measurements do not establish a latency winner
for every task or meet a one-second first-speech guarantee.

## Compared behavior

- **Directory:** the stable system instruction lists allowed tools and reviewed short
  descriptions. The model calls `inspect_tool`, then `execute_tool`. Native service
  schemas never enter the registered function list. Initial narration streams;
  intermediate tool rounds stay silent. A final tool-free reply is buffered until
  the inference ends so it can be distinguished from another internal step.
- **Selective:** the retained selector preloads matching schemas for an explicit
  service request, or enables them through `search_tools`. The model calls selected
  service functions directly. Selected schemas persist across follow-ups within
  the existing count and size limits. Its intermediate narration remains audible.

Both use identical model and TTS settings, canonical argument validation, connection
ownership, integration execution locks, allowlists and authentication. Neither
replays an action after interactive authorization. Authorization completion updates
the catalog and triggers another model inference.

The [reviewed inventory](tool-directory.md) contains 66 callable service entries.
The directory was 5,199 UTF-8 bytes; full specifications arrive only when inspected.
Fetched specifications and results remain in conversation history. The implementation
does not compact history or prove that Ollama reused a prompt cache.

## Three-repeat comparison

```sh
uv run python -m scripts.benchmark_tools \
  --modes directory,selective --repeats 3 \
  --cases greeting,calendar,docs,discovery,warm_greeting \
  --output /tmp/luna-strategies.json
```

Five cases per strategy, repeated three times with alternating strategy order:
**30/30 cases returned correct service arguments and generated spoken responses**.
No recorded errors or detected tool payloads in speech. The cases exercise greeting,
calendar arguments, a Docs edit followed by verification, an agenda request without
an explicit service name, and a subsequent greeting.

The LLM, Pipecat and TTS are real. Google execution uses a local HTTP MCP fixture
with the connected servers' real allowlisted input schemas and synthetic responses;
the document edits do not reach Google. Inputs are typed, with no microphone or
playback in this comparison. Each case starts a fresh conversation. The subsequent
greeting follows service cases to exercise a matching prefix, not retained history.

Times below are medians in seconds, three samples per cell. First audio measures
the first generated TTS audio, not the moment a user hears speaker playback. Total
measures completion through TTS; microphone capture, STT and turn detection are excluded.

| Request | Directory first audio | Selective first audio | Directory first service result | Selective first service result | Directory total | Selective total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Greeting | 0.983 | 0.998 | — | — | 1.027 | 1.044 |
| Calendar | 2.798 | 16.868 | 14.156 | 14.096 | 22.717 | 20.948 |
| Docs edit and verification | 2.754 | 2.149 | 9.739 | 6.118 | 20.223 | 12.547 |
| Agenda without service name | 2.685 | 2.688 | 14.147 | 19.301 | 22.591 | 23.631 |
| Subsequent greeting | 2.285 | 2.384 | — | — | 2.331 | 2.429 |

The directory produced initial acknowledgements in eight of nine service cases and
kept every intermediate tool round silent. Selective spoke between Docs tool calls
in all three runs. Calendar first audio improved because directory narrated before
inspection; the actual calendar result arrived at almost the same time.

Extra inspection has a cost: Docs required five inference rounds with directory
and three with selective. Directory's final-answer buffering also delays speech.

Variability matters: directory greeting first audio ranged **0.917–8.172 seconds**;
its Docs first audio ranged **2.746–18.110 seconds**. The slow Docs run omitted the
initial acknowledgement. Selective agenda first audio ranged **2.631–20.333 seconds**.
Prompting does not force an acknowledgement. Three repetitions are insufficient
for percentile or reliability claims.

The final comparison preceded a minor instruction grammar cleanup and a
behavior-preserving shared-controls refactor. Subsequent focused and full checks
passed; the matrix was not repeated for those changes.

## Real Spotify check

```sh
uv run python -m scripts.benchmark_tools --live-spotify --audible --modes directory,selective \
  --output /tmp/luna-spotify.json
```

One run per strategy, directory first. The entire configured directory stayed
available, while an execution guard allowed only `spotify.getNowPlaying`.
Both used the actual local Spotify MCP server and existing authorization; both
reported that nothing was playing and produced speech through native Mac output.
No microphone was opened. This checks read-only execution, not a fresh Spotify login.

| Strategy | First audio | First service result | Total | Inferences |
| --- | ---: | ---: | ---: | ---: |
| Directory | 8.729 s | 15.367 s | 19.325 s | 3 |
| Selective | 7.781 s | 6.732 s | 10.670 s | 2 |

Directory narrated before inspection, then executed silently. Selective went
directly to the playback tool. Both returned `completed`, used correct arguments,
and had no recorded errors or detected tool payloads in speech. These are single
samples; startup, prefix changes and model behavior can affect their timings.

Google discovery succeeded for all eight endpoints. A live profile read returned
`failed` / `integration_unavailable`; no live Google task succeeded in this A/B.
The exact current failure reason was not established. Prior project verification
reported a Developer Preview enrollment requirement, which should be rechecked
before attributing this failure to it.

## Verification and reproduction

```sh
uv run python -m unittest discover -s tests -q
LUNA_LIVE_TESTS=1 uv run python -m unittest \
  tests.test_mcp_pipeline.PipelineTests.test_live_qwen_and_sentence_tts -q
LUNA_TOOL_STRATEGY=selective uv run python main.py
```

The final default suite ran 52 tests: 51 passed and one opt-in live test was skipped.
The separate real Qwen/VoiceStudio authorization test passed in 50.727 seconds,
covering continued conversation during login, completion-triggered inference,
inspection/execution and speech separation against a mock authenticated MCP server.
An earlier live attempt timed out before the test observed active login; the
observer now also recognizes structured authorization progress. That change and
the passing rerun do not establish the original timeout's cause.

The checks cover protected discovery, refreshed publication, consumed inspection,
conversation isolation, allowlists, canonical validation, tool-speech suppression,
interruption and authentication lifecycle behavior. No custom agent loop was added.

Run artifacts from this session are local temporary files:

- `/private/tmp/luna-strategies-final.json` and `.log`: the 30-case comparison.
- `/private/tmp/luna-strategies-spotify.json` and `.log`: real Spotify runs.
- `/private/tmp/luna-strategies-tests.log`: the final default suite.
- `/private/tmp/luna-directory-live-auth-trace.log`: the passing opt-in authorization check.

Use directory for the requested complete condensed catalog and silent internal
steps. Selective remains a useful comparison and rollback strategy, especially
for tasks where direct invocation saves inference rounds. **Neither meets the
under-one-second target consistently.** A stable directory reduces schema churn;
it does not eliminate inference, argument generation, extra rounds or TTS latency.
