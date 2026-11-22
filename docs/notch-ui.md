# Mac notch shell

Voice mode is a compact indicator attached to the top of the MacBook display.
Saying “Luna” widens it immediately, before Gemini connects. It stays open through
speech, tool calls, and connection recovery. The existing conversation-end event
fires after the final audio drains; that event collapses the panel. Activation
failure and audio-pipeline teardown also clear it. There is no UI inactivity timer.

## Reference and implementation

Research: [TheBoringNotch](https://github.com/TheBoredTeam/boring.notch), specifically
its [native panel](https://github.com/TheBoredTeam/boring.notch/blob/main/boringNotch/components/Notch/BoringNotchWindow.swift),
[shape](https://github.com/TheBoredTeam/boring.notch/blob/main/boringNotch/components/Notch/NotchShape.swift),
and [hover/animation flow](https://github.com/TheBoredTeam/boring.notch/blob/main/boringNotch/ContentView.swift).
It uses a transparent floating panel, a black shape with curved shoulders at the
screen edge, and animated open/closed dimensions. Luna implements that visual
approach independently with AppKit and Core Animation through its existing PyObjC
installation; it does not embed TheBoringNotch or require a Swift app or web view.

`src/ui/notch.py` owns screen selection, geometry, and presentation. macOS's safe
area and auxiliary top areas provide the physical notch dimensions. The panel
prefers the notched display, follows display changes, and joins desktop Spaces and
full-screen apps. On a display without a notch, it collapses completely into the
top edge. Voice mode adds about 48 points on each side and keeps the physical notch's
height (32 points on displays without a notch). A damped spring drives width,
height, corners, controls, composer, and content fades together at 60 Hz. Opening
uses a 0.42-second response and 0.82 damping; closing is critically damped.
Reversing a transition preserves position and velocity. Reduce Motion settles
immediately. Voice mode passes mouse clicks through and never
takes keyboard focus.

## Glass and the notch

The notch is treated as the eclipsed moon, and Luna as the light around it.
`src/ui/glass.py` builds the island's layers. The camera housing is opaque, so the
top band, exactly as tall as the physical notch, stays solid black. Below it, a
gradient over 66 points turns black into frosted glass: an `NSVisualEffectView`
(HUD material, behind-window blending) masked to the island's animated path, under
a dark tint of 50 to 30 percent, so the desktop shows through. Content keeps a 12-point margin from the island edge. A 1-point white edge (16 percent) fades in below
the band, so the hardware edge never shows a seam. The canvas keeps 48 points on
each side and 72 below the island for the corona; clicks there pass through.

The corona is a blue, violet and teal gradient edge plus a soft violet light that
pools beneath the island. It eases in over about 0.25 s while Luna speaks or works
on a typed reply, follows her voice level while speaking, and disappears at rest
(instantly with Reduce Motion).

Luna's mark, the Diamond Ring, sits in the left wing: a thin ring with one bright
bead. The bead carries state. It grows with your voice while listening, circles the
ring every 1.4 s while a typed reply is in progress (it holds still with Reduce
Motion), turns violet while Luna speaks, and turns amber after a failed reply. In
voice mode a soft line in the right wing follows the audio, white for the microphone
and violet for Luna's playback, rather than audio queued by the model. Playback
completion and interruption return it to the microphone. The line is drawn from five
levels, each the RMS amplitude of a consecutive slice of PCM mapped from -55 to
-15 dBFS with a short attack/release envelope; silence settles to a near-flat line.
Audio buffers are neither retained nor sent to the UI: only five normalized levels,
their source, and a monotonic timestamp cross the thread boundary. Ambient levels are
not published before wake activation.

`src/ui/app.py` runs AppKit on the main thread and the existing voice pipeline on
an asyncio thread. A queue transfers visibility updates to AppKit. Ctrl+C/SIGTERM
requests normal agent shutdown and waits for audio, integrations, and memory to
close. Startup exceptions propagate to the launcher.

## Text conversation

The text UI follows one design language: SF Pro on a fixed scale (15 field, 13.5
body, 12.5 secondary, 11.5 hints), text columns 24 points from the island edge,
44-point rows, concentric corners (island, then 14 for surfaces, then 8 for controls),
white overlays of 6% (surfaces), 12% (hover) and 16% (selected), and violet only for
Luna's state, focus and the open chat. Amber marks problems.

**Compact first.** Hover over the island for 180 ms to reveal only the composer,
440 points wide, with history, new chat and pin in the notch's right wing. Hover
always opens this compact state, even when the current chat has messages; the full
chat window opens only after you send a message or pick a chat. Once the island
collapses (and is not pinned), the next hover starts compact again. New chat returns
to the compact composer.

**Open field composer.** 15-point text sits directly on the glass over a hairline,
with no box. The hairline turns violet while the field holds text. On the right are
attach (the standard Open panel), a Think toggle (its icon takes the selected fill
when on; saved in the history database as `brain`) and a Return keycap that lights
up when the message can be sent. The keycap sends on click, Enter sends, and
Shift+Enter inserts a line break. The field looks the same while Luna replies, so you
can type the next message. Attachments are 28-point surface chips above the field.
The placeholder is "Ask Luna anything" when compact and "Reply to Luna" in a chat.

Hover alone does not take keyboard focus, but it does show cursors: the I-beam
over the fields and a pointing hand over every enabled button and history row.
AppKit's cursor rects and cursor-update areas work only in the key window or active
app, so always-active enter/exit tracking areas set these instead. One control owns
the cursor at a time; disabling, hiding or collapsing returns the arrow. Buttons and
rows also fade in the hover overlay over 0.16 s (instant with Reduce Motion), above
any state fill. The prompt and the search field accept the first click, so a single
click both focuses the panel and places the caret. Standard Mac editing shortcuts work.

**Full chat.** Sending or picking a chat smoothly grows the island to 520 × 480
points (bounded by the display). New chat and pin glide from the wing into a toolbar
beside the chat title; the title's chevron is a text attachment centred on its cap
height. Your messages are surface cards on the right; Luna's replies are plain text
on the same column as the field. Tools Luna used appear above her reply as one quiet
line ("✓ Used Calendar · 2 steps ›") that expands into the steps. The agent reports
each call as a service and a short label, never its arguments or results, and the
steps are saved with the reply. Labels use the title the tool's MCP server provides;
tools published under a hashed name show only their service, and plumbing
(reconnecting a service, paging a long result) is not listed. Native cells measure
wrapping, messages are selectable, and the transcript follows new output only when
already near the bottom.

Replies stream from the text agent (`src/agent/text.py`). Think starts with Gemini
3.8 Flash and Quick (Think off) with Gemini 3.5 Flash-Lite, falling back as described
in the README. Model names, model IDs and server names never appear in the UI. A
failed reply shows its plain-language reason in amber in place of the reply, with a
Try again button that asks again in place: the same user turn and reply row, with
no duplicate question. The notch's bead turns amber until the next good reply.

**History (typeahead).** The history button, or the chat title, turns the field into
search: a magnifier, "Search chats" and an Esc keycap that returns to writing in the
size the island had before. History is always compact. With an empty field it shows
your last three chats with times, the open chat marked by a violet dot; chats with no
messages are never listed. Typing completes the closest chat title in grey (Tab
accepts) and lists matching chats under "Chat" and chats whose messages mention the
words under "Mentioned in", with a one-line snippet; matches are bold. ↑/↓ move the
selection, Return opens it, and the island grows or shrinks with the results. Search
covers titles and message text (`ChatStore.search`, an escaped `LIKE` query).
Times follow the system's 12- or 24-hour setting.

The pin fills and takes the selected fill when active. Moving away for 350 ms
collapses an unpinned conversation; a pinned one stays open. Escape in the composer
unpins and dismisses the text view, suppressing another hover-open until the pointer
leaves. While text is open it takes visual priority over voice; leaving it returns
to the voice indicator if a voice exchange is still active. A voice-end event never
closes a hovered or pinned text conversation.

Chats, drafts, and local file references persist in
`~/Library/Application Support/Luna/text-chats.sqlite3` with private file
permissions. Drafts save after a short debounce and before chat switches or shutdown.
A failed save preserves the composer and shows an amber status line above the field.
Only one GUI instance may open this history at a time.

Drag readable local files toward the island to open the compact island as a drop
target: a dashed violet edge with 14-point corners, "Drop to attach" and "Files go
with your next message". Dropping adds removable chips above the field. They belong
only to the next sent message, then appear on that message as file rows. Dropping
during a reply queues files for the following message. Duplicates are ignored;
folders, remote file URLs, missing files, and file promises are unsupported. The app
stores references and file names; it does not move, upload, or read file contents.

The drag flow follows TheBoringNotch's
[drag-pasteboard detection](https://github.com/TheBoredTeam/boring.notch/blob/b7611bbf32c627a3427408fc142486900d71a23f/boringNotch/observers/DragDetector.swift)
and [shelf drop behavior](https://github.com/TheBoredTeam/boring.notch/blob/b7611bbf32c627a3427408fc142486900d71a23f/boringNotch/components/Shelf/Services/ShelfDropService.swift).
A 450 ms drag-exit grace keeps the destination available while the shape changes.
Its [shared spring approach](https://github.com/TheBoredTeam/boring.notch/blob/b7611bbf32c627a3427408fc142486900d71a23f/boringNotch/ContentView.swift)
also informs Luna's coordinated motion. Luna implements these patterns
independently through AppKit; no upstream source is embedded.

`src/ui/notch.py` owns presentation state and hover/pin/drop arbitration. A fixed
maximum-size native window lets its shape morph while its top edge stays anchored;
the background and content share a shape mask. Mouse events pass through
outside the current interactive shape. `src/ui/glass.py` owns the glass layers, the mark and the
voice line. `src/ui/chat.py` owns the open-field composer, message layout, step
lines, inline errors and retry, attachments, and typeahead history.
`src/ui/chats.py` owns SQLite history, including each reply's status
(`pending`, `complete`, `failed`, `interrupted`) and answering model; replies still
pending at startup become `interrupted`. `src/ui/motion.py` owns retargetable springs.
Message growth is eased while text remains anchored to the bubble's top edge.
The existing AppKit timer drives hover/drop detection and motion and drains agent
events; there are no global input hooks or additional permissions. `ChatContent`
puts a `TextRequest` (prior completed turns, text, file paths, brain flag) on a
queue for the `luna-agent` thread. `TextAgent.serve` answers each request as a task
and returns `TextEvent`s through the existing UI event queue; only text crosses
threads, and SQLite stays on the main thread. Text chats run beside the voice
pipeline and survive its rebuilds. They share its MCP runtime and memory. Attached
files are named to the model but never read or uploaded, and text replies are never spoken.

Run `uv run python main.py --preview` for an interactive UI-only session with no
microphone, model, integrations, or memory service; each send gets a failed reply
explaining that preview mode doesn't connect to Luna's assistant. Stop it with Ctrl+C in the
launching terminal. Normal startup connects the same text UI to the agent alongside
the existing voice assistant. Hold-to-talk remains deferred.
