# Mac notch shell

Voice mode is a compact black indicator attached to the top of the MacBook display.
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

A white SF Symbols moon sits in the left wing; five rounded bars sit in the right.
A matching animated mask reveals the wings during expansion and hides their
contents during collapse. White bars show the echo-cancelled microphone while
listening. A blue-to-purple gradient shows Luna's actual native playback, rather
than audio queued by the model. Playback completion and interruption return the
indicator to the microphone. Each bar measures the RMS amplitude of a consecutive
slice of PCM, mapped from -55 to -15 dBFS, with a short attack/release envelope.
Silence settles to small dots. Audio buffers are neither retained nor sent to the
UI: only five normalized levels, their source, and a monotonic timestamp cross the
thread boundary. Ambient levels are not published before wake activation.

`src/ui/app.py` runs AppKit on the main thread and the existing voice pipeline on
an asyncio thread. A queue transfers visibility updates to AppKit. Ctrl+C/SIGTERM
requests normal agent shutdown and waits for audio, integrations, and memory to
close. Startup exceptions propagate to the launcher.

## Text conversation preview

Hover over the island for 180 ms to reveal a compact composer: the same voice
width, extended 80 points below its usual height. A history button replaces the
waveform. Apple's [SF Symbols](https://developer.apple.com/sf-symbols/)
`clock.arrow.circlepath` keeps the same rendering as the moon without another
dependency.
Hover alone does not take keyboard focus. Clicking the composer enables editing.
Enter sends, Shift+Enter inserts a line break, and standard Mac editing shortcuts
work in the text view.

Sending smoothly widens and deepens the island to 520 × 440 points (bounded by
the display). The moon stays attached to its left corner. A pin appears next to
the history and new-chat buttons on the right. Gray user bubbles align right; blue-to-purple Luna
bubbles align left. Native cells measure text wrapping, messages are selectable,
and the native scroll view follows new output only when already near the bottom.
The composer stays at the bottom. Replies start with a short typing pause and
stream into the conversation; duplicate sends are disabled during that reply,
while typing the next draft remains available.

The pin gains a purple background when active. Moving away for 350 ms collapses
an unpinned conversation; a pinned conversation stays open. Escape unpins and
dismisses the text view, suppressing another hover-open until the pointer leaves.
Reopening returns to the existing conversation. While text is open it takes visual priority
over voice; leaving it returns to the voice indicator if a voice exchange is still
active. A voice-end event never closes a hovered or pinned text conversation.

The history button crossfades into a scrollable conversation picker with titles,
previews, and the selected chat highlighted. Selecting a chat restores its draft
and queued files. The compose button starts a new conversation, retaining the
full-size island so the controls stay under the pointer. Chats, drafts, and local
file references persist in `~/Library/Application Support/Luna/text-chats.sqlite3`
with private file permissions. Drafts save after a short debounce and before chat
switches or shutdown. A failed save preserves the composer and shows a status
message. Only one GUI instance may open this history at a time.

Drag readable local files toward the island to reveal a purple drop target.
Dropping adds removable file chips above the composer. They belong only to the
next sent message, then appear on that message as file cards. Dropping during a
reply queues files for the following message. Duplicates are ignored; folders,
remote file URLs, missing files, and file promises are unsupported. The app stores
references and file names; it does not move, upload, or read file contents.

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
outside the current interactive shape. `src/ui/chat.py` owns the native composer,
message layout, attachments, history picker, and deterministic mock reply stream.
`src/ui/chats.py` owns SQLite history; `src/ui/motion.py` owns retargetable springs.
Message growth is eased while text remains anchored to the bubble's top edge.
The existing AppKit timer drives hover/drop detection, motion, and replies; there
are no global input hooks or additional permissions. There is no text backend,
tool execution, file upload, speech output, or connection to agent memory in this
preview. The older typed audio diagnostic
remains a separate tool.

Run `uv run python main.py --preview` for an interactive UI-only session with no
microphone, Gemini, integrations, or memory service. Stop it with Ctrl+C in the
launching terminal. Normal startup also exposes the same mocked text UI alongside
the existing voice assistant. Hold-to-talk remains deferred.
