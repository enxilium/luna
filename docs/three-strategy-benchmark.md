> Historical October 2026 investigation. Luna now uses Gemini Live; see [the current migration](gemini-live.md). Old commands, source paths and timing claims below refer to the superseded local-model implementation.

# Directory, selective and private executor

Historical comparison. Luna now uses selective publication exclusively; directory
and executor implementations and strategy switches have been removed. Commands below
describe the measured checkout and are not supported by the current runner.

Measured October 2, 2026 on this Mac with local `qwen3.8:27b-q4_K_M`, Ollama 0.35.0,
VoiceStudio sentence TTS, Pipecat 1.12.0 and MCP SDK 2.1.1. No model, temperature,
TTS engine, integration allowlist or upstream server was changed for this comparison.

## Strategies

- **Directory:** Luna sees reviewed tool names and short descriptions. It inspects
  schemas before each service execution. Initial narration is audible; intermediate
  tool rounds are silent.
- **Selective:** the existing selector publishes a small set of matching service
  schemas. Luna calls them directly, retaining them across follow-ups. Its intermediate
  narration remains audible.
- **Executor:** Luna sees the same directory and delegates a complete task with
  selected tool names and resolved context. A fresh private Pipecat worker loads only
  those schemas and executes the dependent calls. It returns a compact outcome,
  completed tool names and useful facts. Its full schemas, intermediate results and
  text do not enter Luna's conversation or TTS.

The executor uses the same loaded Qwen model. Parent and worker share an inference
lock, so requests remain serial even during user interruptions or parallel delegations.
MCP sessions remain owned by the existing runtime, rather than by disposable workers.
No custom agent loop, new model, dependency or separate user-visible chat was added.
Directory remains the default; this comparison does not automatically promote a strategy.

## Measurements and correctness

```sh
uv run python -m scripts.benchmark_tools \
  --modes directory,selective,executor --repeats 3 \
  --output /tmp/luna-three-strategies.json
LUNA_TOOL_STRATEGY=executor uv run python main.py
```

Three repetitions of five cases for each strategy, alternating forward/reverse
strategy order. This puts selective in the middle; it is not a fully balanced or
randomized experiment. Each case has a fresh parent conversation except the seeded
history case. Focused tests against local mock servers ran during part of this
comparison, so machine load was not isolated; no competing real-model benchmark
was launched. The model was already loaded by the preceding smoke test, but no
prompt-cache hits were measured or guaranteed.

The three primary metrics are:

1. **First audio:** first generated TTS audio after the typed request enters the
   pipeline. This may be an acknowledgement, not the final answer.
2. **Task complete:** the terminal response finishes generating its audio, preserving
   the meaning of total completion in the earlier A/B. A failed correctness check
   has no successful completion time; its elapsed turn time is still recorded.
3. **Task correctness:** successful service effects and requested arguments, a
   completed spoken response, and no calls outside the case's permitted actions.

Service completion is recorded separately: when the final required service operation
has finished. For playback, this is successful play dispatch after search; for Docs,
it includes a verification read after the edit. Calendar correctness additionally
checks that the spoken text contains all five returned event titles. Docs checks
one correct replacement in the specified document and a subsequent verification.
Playback checks search for both title and artist, followed by exactly one play call
with the returned original-recording URI/ID. A wrong cover/live recording, omitted
search, duplicated play or duplicated edit fails the check.

The LLM, native Pipecat tool machinery and TTS are real. The MCP fixture exposes the
connected servers' real allowlisted input schemas, but its Google data and Spotify
playback responses are synthetic. No document edit or playback change reaches an
external service. The recording choices include the correct original, a live version
and a cover. `history_playback` seeds 13 user/assistant exchanges, including the
favorite-song fact, and asks Luna to resolve that reference before delegating.
That history contains 26 messages and 9,461 characters; this tests one moderate
history length rather than lifetime context growth.

These checks cover the specified tasks; they are not a broad tool-use quality score.
Typed input bypasses microphone capture, STT and turn detection. The main comparison
does not play sound. Generated-audio times exclude speaker scheduling and the duration
of listening to the reply. Playback dispatch is not a measurement of physical music onset.
Three samples per case do not justify percentile or reliability guarantees.

## Results

**45/45 cases met the defined correctness checks**, 15 per strategy. No recorded
tool errors or detected tool payloads in speech. Times below are medians in seconds,
three samples per cell. Task completion includes the final generated reply.

| Case | Strategy | First audio | Task complete | Correct |
| --- | --- | ---: | ---: | ---: |
| Greeting | Directory | 8.202 | 8.245 | 3/3 |
| Greeting | Selective | 1.305 | 1.358 | 3/3 |
| Greeting | Executor | 2.760 | 2.812 | 3/3 |
| Calendar | Directory | 3.108 | 23.979 | 3/3 |
| Calendar | Selective | 13.924 | 16.192 | 3/3 |
| Calendar | Executor | 3.101 | 57.303 | 3/3 |
| Docs edit and verification | Directory | 2.863 | 21.071 | 3/3 |
| Docs edit and verification | Selective | 2.298 | 13.587 | 3/3 |
| Docs edit and verification | Executor | 3.953 | 35.999 | 3/3 |
| Song search and playback | Directory | 3.439 | 22.769 | 3/3 |
| Song search and playback | Selective | 3.852 | 15.143 | 3/3 |
| Song search and playback | Executor | 3.540 | 33.180 | 3/3 |
| Song resolved from history | Directory | 9.457 | 29.158 | 3/3 |
| Song resolved from history | Selective | 9.695 | 19.706 | 3/3 |
| Song resolved from history | Executor | 9.529 | 36.698 | 3/3 |

Service completion occurs before that closing reply:

| Service work | Directory | Selective | Executor |
| --- | ---: | ---: | ---: |
| Calendar result available | 15.020 | 7.720 | 21.722 |
| Docs edit verified | 17.614 | 10.263 | 22.399 |
| Playback dispatched after search | 20.666 | 12.560 | 20.529 |
| Playback dispatched with history | 26.916 | 17.684 | 25.240 |

The executor's median completion was slower in every service case. It kept the
parent context small, but added task-description generation, private outcome
generation and a handoff back to Luna. Calendar required four total inferences
with executor versus three with directory and two with selective. Playback and
Docs required five with executor, five with directory and three with selective.
Only two executor inferences were in the parent; the others were in the private worker.

The first calendar sample had the same 195-character final answer for directory
and executor, while executor additionally generated a 192-character private summary
and 372 characters of facts. The slower turn therefore cannot be attributed just
to a longer user-facing answer. We did not capture per-request prompt-evaluation
durations or cache-hit counts, so the contribution of cache switching remains unproven.

None consistently meets a one-second first-audio target. Greeting first audio
ranged 2.708–8.671 seconds for directory, 1.031–4.779 for selective and 2.300–8.503
for executor. Calendar executor completion ranged 53.544–63.137 seconds. The seeded
history case produced roughly nine-second first audio for all three strategies;
discarding the worker's history does not remove the parent's initial context work.

**Keep executor available for comparison, rather than choosing it for latency on
this model.** Selective completed these tasks fastest; directory and executor more
consistently narrated before calendar work. The sample is too small to rank general
tool quality, and all three scored equally on the specified correctness checks.

## Real Spotify read-only check

One actual `getNowPlaying` call per strategy, in directory/selective/executor order,
using existing authorization and native audible output. The execution guard allowed
only that read; it preserved the whole configured directory. All calls returned
`completed` and generated speech, with no recorded errors or detected tool payloads.
Playback metadata changed as the user's existing music continued, so these are
single operational checks with variable response content, not a controlled latency ranking.

| Strategy | First audio | Task complete | Service result available |
| --- | ---: | ---: | ---: |
| Directory | 8.364 s | 35.561 s | 15.132 s |
| Selective | 9.208 s | 22.175 s | 6.696 s |
| Executor | 2.724 s | 39.862 s | 10.051 s |

The automatic live correctness flag checks successful execution and generated
speech; it does not independently score every spoken metadata field. No fresh
Spotify login or actual song-start timing was tested. Live Google task execution
remains unavailable in prior verification; this comparison used Google fixtures.

## Lifecycle verification

```sh
uv run python -m unittest discover -s tests -q
LUNA_LIVE_TESTS=1 uv run python -m unittest \
  tests.test_mcp_pipeline.PipelineTests.test_live_qwen_executor_and_sentence_tts -q
uv run python -m scripts.benchmark_tools --live-spotify --audible \
  --modes directory,selective,executor --output /tmp/luna-three-spotify.json
```

Focused tests cover fresh/scoped workers, native chained calls, compact results,
auth-required return without login or replay, clarification, cancellation, timeout,
shutdown, shared serial inference, and attempts to invoke an unselected tool.
Native parent pipelines cover protected discovery, background authorization,
interruption-safe login, explicit auth cancellation and completion-triggered delegation.
Private worker text does not reach parent TTS.

A boundary test exposed that assistant tool-result context frames travel upstream
directly into the LLM, bypassing an earlier upstream processor. The inference-budget
check now lives at the service's context-entry boundary and stops before a seventh
request. Workers allow at most six inference rounds, eight service calls and 120 seconds;
the native parent delegation deadline is 125 seconds to leave cleanup time.

Ordinary authentication requirements stop the worker and return to Luna. It cannot
open a browser or resume itself after authorization. The existing authorization
tool refreshes schemas and triggers parent inference; prompting decides the next task.
Completed action names accompany partial outcomes so Luna can avoid repeating edits.
The runtime never automatically replays an action after login.

The final default suite ran 65 tests: 63 passed and two opt-in live tests were skipped.
The fixed inference-boundary checks also passed all nine focused executor/benchmark
tests. The comparison was already running when its metric field names were clarified
and the boundary guard was moved. All compared worker tasks stayed below the budget;
the final JSON separates service completion from full-turn completion using the
original timestamps. No samples were removed and recorded timestamps were not changed.

**The additional live executor authorization check did not pass.** Two runs of the
implemented prompt timed out after receiving `auth_required`; the diagnostic rerun
recorded only `connect_integration` being called. Qwen narrated that sign-in was
starting without invoking `authorize_integration`, so neither interactive login nor
the private worker began. A more explicit prompt was tried once and produced the
same omission; that ineffective wording was removed. Request-schema checks verify
that authorization remains advertised, including its integration ID, on the next
upstream inference. Mock authorization/lifecycle flows pass, but this live model
follow-through remains unverified and unreliable. This failure is separate from
the 45 already-authorized task cases and the real Spotify reads using saved credentials.

Run artifacts:

- `/private/tmp/luna-three-strategies.json`: final 45-case data with both completion clocks.
- `/private/tmp/luna-three-strategies-service-clock.json`: original data before field clarification.
- `/private/tmp/luna-three-strategies.log`: raw execution log; its initial `task_complete_seconds` field means service completion.
- `/private/tmp/luna-three-spotify.json` and `.log`: the actual read-only Spotify checks.
- `/private/tmp/luna-three-strategies-final-tests.log`: final default suite.
- `/private/tmp/luna-executor-boundary-final.log`: focused boundary checks.
- `/private/tmp/luna-executor-live-auth.log`: opt-in Qwen executor authorization check.
- `/private/tmp/luna-executor-live-auth-rerun.log`: diagnostic rerun of that check.
- `/private/tmp/luna-executor-live-auth-final.log`: the unsuccessful prompt-clarification trial.
