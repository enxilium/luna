# Personal dashboard

The dashboard is Luna's window: a frosted glass window, the same material as the
notch, that a global shortcut (⌃⌥Space by default) brings to the front from any app. A floating top bar switches between Home, Projects, Academia and Settings. It runs
inside Luna (`src/ui/app.py`), in the same process as the notch and the voice pipeline,
hidden until its shortcut or until you click Luna in the Dock. It doesn't call the agent: no
memory calls, and Gemini only for today's picks.

```sh
uv run python -m scripts.build_app && open build/Luna.app   # all of Luna, your data, as its own app (see below)
uv run python main.py --demo               # all of Luna from a shell, sample dashboard data
uv run python main.py --dashboard          # the dashboard alone, your data, from a shell (no Calendar access possible)
uv run python main.py --dashboard --demo   # seeded sample data, in memory and a temp folder
uv run python -m src.dashboard.server --demo --port 8790 --token dev   # the UI in a browser, for development
```

The browser version (`http://127.0.0.1:8790/?token=dev`) has no desktop behind it,
so it paints a still of your wallpaper and blurs it with CSS `backdrop-filter` to
imitate the window. For a rotating wallpaper folder that still is the folder's first image.

## The window

```
NSWindow: titled, clear, normal level; Luna is a regular app with a Dock icon
├─ NSVisualEffectView: HUD material, behind-window blending (the desktop frosts through the whole window)
├─ tint layer: black at the Settings tint; Clear lowers the blur's opacity, Dark adds more tint
├─ WKWebView: transparent; draws panels (CSS glass) and content
└─ drag strip: moves the window from the top bar, except over its controls
```

- **Shortcut.** `hotkey.py` registers it with Carbon's `RegisterEventHotKey`, which
  works from any app and needs no Accessibility permission. Settings → Overlay offers
  ⌃⌥Space, ⌥Space, ⇧⌘Space, ⌃⌥L and ⇧⌘L. If another app already owns the combination,
  macOS refuses it and the page says so.
- **Showing.** The shortcut or the Dock icon brings the window to the front where you
  left it (un-minimizing it), and the page reloads its data, since it may have been
  closed for hours. It behaves like any window: switching apps, or answering a
  permission prompt, leaves it where it is.
- **Closing.** The close button or ⌘W fades it out and Luna keeps running; the minimize
  button or ⌘M sends it to the Dock. ⌘Q quits.
- **Window buttons.** Close, minimize and zoom sit at the top bar's left, centred on it:
  `place_lights` grows the title bar container to the bar's height (the page reports it with
  the drag holes) and moves the buttons, again on every resize and on leaving full screen,
  because AppKit lays them out for a 28 pt title bar. The page pads the bar's left side for them.
- **Backdrop.** By default the desktop behind the window frosts through. Settings → Overlay →
  Backdrop can choose a folder of pictures instead (`backdrop_folder`, images directly in it):
  the page paints them full-window (`web/js/backdrop.js`), blurred once into a small canvas
  (lighter for Clear) under the Settings tint, crossfading to the next every 1, 5, 10, 30 or
  60 minutes (`backdrop_interval`) in a shuffled order that shows each once per round. The
  browser preview uses the wallpaper still the same way. `/api/backdrop` lists the pictures; `/api/backdrop/image` serves
  only files inside that folder.
- **Panels** are CSS glass over the window's glass: a faint white fill, a 1 px edge
  and a top highlight. Sheets and menus sit over other panels, so they use
  `backdrop-filter` to frost the page content under them. Every glass element carries
  `data-pane` (its role: `stat-1`…`stat-4`, `main`, `side`, `bottom-*`) and
  `data-glass` (`panel`, `pill`, `chip`, `sheet` or `menu`).
- Panels in sized rows lay their body out absolutely, so long lists scroll inside the
  pane and never stretch the grid.
- PyObjC aborts the whole app if a block it hands to AppKit returns a value, so
  completion handlers must return None (`OverlayTests` checks the hide fade).

## Design language

The dashboard is the notch grown into a window. Every surface draws from one palette,
one type scale and one spacing scale (the "Foundations" board of the design canvas);
tokens live at the top of `web/css/glass.css`.

- **Ink.** Five neutrals tinted toward Luna's violet, one per level of importance:
  title `#F6F4FC` (`--ink`), body `#DCD8EA` (`--ink-1`), secondary `#B4AEC9` (`--ink-2`),
  meta `#8F89A7` (`--ink-3`), quiet `#615C77` (`--ink-4`, rules and empty rings only).
  No text shadows.
- **Overlays.** Surfaces are white 4.5% with an 8% edge; hover 9%; selected 12%;
  hairlines 9%.
- **Accents, one job each.** Lilac (`--lilac`, dark ink on it) is the primary action.
  Violet is focus (a field's hairline, the caret) and "in progress". Luna's corona
  (blue → violet → teal) is light for *now and next* only: the timeline's now line, the
  next event's edge, and Luna's picks for today. Due soon elsewhere is brighter ink, not
  corona. Coral is overdue and
  other problems. Amber is "needs your reply" (unanswered invitations). Mint is done.
- **Identity hues.** Eight matched hues (`--hue-iris` … `--hue-sky`, as `r g b`
  triplets) tell work apart before you read it. A project's comes from its category
  (music rose, software lagoon, writing sand, photography peach, 3D orchid, other
  sky); a course picks its own. The hue tints the owner's glyph tile and chip, its
  progress bar and waveform, the owner label on Home's task rows, and its calendar
  blocks. Components set `--h` and use `rgb(var(--h) / α)`.
- **Type.** SF Pro: 28 display (page and document titles), 20 titles, 15 section heads
  and fields you write in, 13.5 body, 12.5 labels, 11.5 meta. Home keeps its greeting
  at 30.
- **Spacing.** 4/8/12/16/24/32/48: 8 inside rows, 16 between rows of controls,
  24 pane padding, 32 between sections. Rows are 40 tall (Home's 44).
- **Shape.** Concentric corners: 14 surfaces, 10 tiles, 8 controls and rows, 6 keycaps
  and chips.
- **Fields are open**, like the notch composer: text on the glass over a hairline that
  turns violet while focused. No boxes.
- **Content is intentional.** No hint lines explaining the UI, no figure repeated on
  every card; empty states say what the thing is for, once.
- **Identity.** The Diamond Ring sits at the top left, and the top 66 points darken
  toward the edge, like the notch's black band turning into glass.
- Pointing-hand cursor on every enabled button and row; the arrow elsewhere.
- **Motion.** Motion explains where you went; what didn't change holds still. Three curves
  (ease-out to enter, ease-in to leave, ease-in-out to move) and four durations (180 ms hovers,
  260 ms small changes, 400 ms sheets and highlights, 600 ms layout), tokens at the top of
  `glass.css`.
  - **Highlights glide; labels never move.** The active tab's pill and the selected rail row
    are separate elements (`indicator()` in `ui.js`) that slide under the items; the tab
    answers a click at once, before the page has loaded.
  - **Navigation is choreographed by meaning** (`transition(update, motion)` in `core.js`,
    picked by `motionFor()` in `app.js`, keyed on `html[data-motion]`): another top-level
    page is a shared-axis slide along the tab order (the old page leaves 40 px toward where
    it came from in 200 ms with ease-in; 60 ms in, the new one glides 40 px in from the other
    side on a 480 ms ease-out while its opacity comes up in 280 ms); opening a
    task, note or event steps forward in depth on the main pane only, and back steps out;
    another item at the same level (the next project, another note) fades through on the
    main pane while the rail stays put; the calendar slides to the next or previous period
    and fades through when its view changes, with Home's left panel still.
  - **Overlap without double exposure.** The new view starts arriving while the old one is
    still leaving, so the window is never empty (combined opacity stays above ~0.65), but the
    two are never both at full strength, and snapshots don't blend (`mix-blend-mode:
    normal`), so translucent glass never brightens. The bar and the backdrop hold still.
  - **No waiting.** After start-up every tab's data is prefetched while idle, so a tab switch
    plays its transition within a frame instead of after the server answers; the page then
    refreshes its cached data quietly.
  - **Writing mode** (a document in Edit mode) slides the rail and the right column off their
    edges while the page widens, all on one 560 ms curve; the page shows its new content as
    its box grows.
  - Sheets rise 16 px and settle; menus grow from where they opened; buttons give a little
    when pressed; a finished task's tick draws, then its row folds away before it's saved.
  - Nothing is filtered live while things move (the backdrop is blurred once into a small
    canvas), so a transition's capture takes milliseconds. Background refreshes never cancel
    a navigation, and a window that isn't painting still updates, unanimated, after 1.5 s.
    Reduce Motion turns transitions and gliding highlights off.
  - Measured in WebKit (the app's engine), not only Chrome: WebKit has no canvas filters, so
    the backdrop's blur is a three-pass box blur on its small canvas.

## Pages

| Page | What's there |
| --- | --- |
| Home | A sheet (greeting, weather, what's next, Overdue, Upcoming Tasks, Waiting on you) and a desk (project cards, calendar pane with day, week and month views) |
| Projects | A rail (pinned, active, archived) beside the open project: category chip, name, description, progress (finished over all tasks, repeating ones aside), tasks by status with an inline add, and a side column with the latest work from the folder (mix, commits, words, photos), the project's notes and its newest files. Tasks and notes open in place in the shared editor |
| Academia | Courses (code, colour, icon) with their notes nested in the rail; the open note, task or course in the middle; the course, what's coming up (important events always lit, the next one in corona) and its open tasks on the right |
| Settings | Overlay shortcut, glass style and tint, backdrop, calendar and location access, message sources, your name, Run setup again, and where data lives |

**Setup** (`#/setup`, `web/js/pages/setup.js`, `src/setup.py`) is the one page outside the top
bar. Luna opens on it by itself (and the window shows at launch) while voice can't
start: no Gemini key or no wake word model. Seven steps, one card at a time, with the
steps above it (violet: where you are, mint: done): Welcome (your name), Gemini (paste a
key from AI Studio; `POST /api/setup/key` checks it with Google and keeps it in the
Keychain, or says why not), Microphone (`POST /api/setup/microphone` asks macOS once),
Wake word (`POST /api/setup/wake-model`; it starts by itself when setup opens, with
progress over the `setup` event), Extras (Calendar, Location, Messages, all optional),
Connections (memory, Google, Spotify, Canvas, all optional: `POST /api/setup/connect/<name>`
starts `src/install/<name>.py` in the background, its step and progress arrive as `setup`
events, and failures as reason codes the page words; Canvas takes the school's address and
a token; a row says "Not in this copy" when the app wasn't packaged with what it needs;
voice restarts by itself to pick each one up) and Ready (the three ways to call Luna). While it's open the page pins the window
(`{type: "pin"}` over the bridge): it drops to a normal window level and doesn't hide
when you switch apps, since setup sends you to the browser and System Settings.
"Start using Luna" goes Home and hides the window; from then on Luna stays in the
background.

The top bar also holds Messages (an inbox and conversation sheet with live updates),
notifications (overdue tasks, birthdays), search (⌘K: projects, courses,
tasks, notes, events and more, each opening where it lives) and your initials. ⌘1–⌘4
switch pages, ⌘N creates (a task, project or course), ⌘, opens Settings, and
Esc closes the top sheet.

Habits, workouts, health metrics, journal, money and people are still stored and
served (`/api/fitness`, `/api/journal`, `/api/money`, `/api/people`, …). People's
birthdays appear on Home. Their pages will come back with the side navigation.

## Home: sheet and desk

Home is the first thing open in the morning. It's a sheet and a desk: the sheet (a glass
pane on the left, 380 px) is you and today; the desk (the rest) is your work. Both use the
same glass panes as the other pages. Type steps: greeting 30 pt, temperature 26, project
titles 14, section heads 13.5, rows 13, meta 12.

- **The sheet**, top to bottom: the date; "Good morning/afternoon/evening, <first name
  from Settings → Your name>" (with no name set, a quiet "Add your name" link opens Settings); the weather (SF Symbol, the temperature, the condition and today's high
  / low; hover for the chance of rain); and one line on what's next ("Next: CSC311 at
  11:00 AM, in 40 min"; after 5 PM with nothing left, "Tomorrow starts with …"; otherwise
  no line, since the calendar already shows an empty day). It's about now, whatever the calendar shows. Until location is
  allowed the weather is a quiet "Show weather" button; with location off it's left out
  (see Weather below). Under a hairline come Overdue, Upcoming Tasks, Completed and Waiting on you. The
  sheet scrolls on its own. It doesn't list today's events: the calendar already does.
- **Pick up where you left off**, at the top of the desk: four glass cards, two by two.
  Pinned projects first, then the most recently touched open ones (archived never). Each
  card wears its category's hue and shows what you'd reach for next:
  - Music: the latest export's name and age, with play and a full-width waveform (stems,
    samples and recorded takes don't count as exports).
  - Software: branch, uncommitted changes ("clean" when none) and this week's commit
    count; the last two commits (hash, subject, age).
  - Writing: total words, the newest document and when; this week's gain ("+1,100 words
    this week") beside a trend line of the daily word counts (last 14 readings).
  - Photography (or a project labelled Design): how many images and the last export;
    thumbnails of the four newest (web formats only).
  - A project labelled Video: the latest render. Anything else kept in git shows its
    commits; otherwise the newest file.
  - No folder linked: the card says what linking would give you, in that category's terms
    ("Play your latest mix", "See your last commit", "Follow your word count"), with a
    Link folder button that opens the folder picker, saves it, and scans. A folder that's
    gone says so in coral with "Choose again".
  Click a card to open the project. "Touched" is the latest of the project's edit, its
  newest item, its newest file and its last commit. Hidden when there are no projects,
  and the calendar takes the desk.
- **The calendar pane** fills the rest of the desk. Its head: what it shows ("Wednesday,
  November 11 · 2 events", "Nov 9 – 15 · 2026 · 8 events", "November 2026 · 13 events";
  counts are calendar events, timed and all-day), "Connect Calendar" when needed, then
  `‹ Today ›` and the Day / Week / Month switch (Day by default, remembered in the
  window). The arrows step the view (a day, a week, a month; months keep the day of the
  month, clamped); the middle button returns to the period holding today and is named for
  it ("Today", "This Week", "This Month"), with a fixed width, dimmed when you're there.
  Calendar events only, never tasks. Day and Week hold all 24 hours but open fitted to the
  hours that matter: an hour around the shown events (and now, when today is shown and
  that doesn't stretch the span past 16 hours), at least eight hours, with the hour height
  (`--hour`, 28 to 72 px) set to fill the pane. Everything else is a scroll away, and
  every hour label carries AM/PM. Resizing keeps the same time at the top. The overlay can
  render while hidden, when the grid has no height: the first scroll waits for a real size.
  - **Day**: events are filled blocks; overlapping ones share the width; elapsed ones dim.
    A thin corona line with the Diamond Ring marks where you are (no clock; it runs under
    events and moves every 30 seconds). The next event gets the corona edge; hovering an
    upcoming event shows how far off it is ("in 40 min", or "Now"). All-day events
    and birthdays sit in an "All day" row above.
  - **Week**: the week's days (weekday and date, today white as "Today", one dot per event,
    task due or birthday, up to three, today's in the corona colours; click to open the
    day) sit over seven columns on the same 24-hour grid, sharing its 56 px hour gutter
    (`--wk-gutter`) so each sits exactly over its column. A month starting mid-week labels
    its 1st with the month. An all-day row sits on top when needed; the now line runs
    through today's column.
  - **Month**: every week touching the month (Monday first), days outside it dimmed.
    Each day lists up to three items (all-day ones first, then events with a compact
    time such as "7:30 PM"), or two and "N more". Click a day to open it.
- **Tasks have a due date, never a time.** There's no scheduling: you don't decide when
  to do a task, you do it by when it's due, so tasks never go on the calendar.
- **Overdue** (only when something is late) and **Upcoming Tasks**: every undone task with
  a due date, however far out, soonest first, priority breaking ties. Undated tasks are a
  backlog and stay off Home. Overdue ones are coral
  (ring, day label and "Due yesterday"); upcoming ones are neutral (a quiet ring and day
  label) unless Luna picked them. The day label shows once per day ("Today", "Sun 15"); each
  row names what it belongs to (project or course, in its hue), "In progress" when started
  (its checkbox is the half-filled violet ring tasks have everywhere), and how it repeats
  ("Weekly"). The check completes it: the tick draws, then the row folds away; the title opens it in its project or
  course (a standalone task opens on its own page). **Add** (or ⌘N) creates a task with a
  due date (the shown day by default), a priority, what it's for, and an optional repeat.
- **Repeating tasks** (daily, weekly, every 2 weeks, monthly; schema v5 `repeat`): finishing
  one creates the next occurrence, due one interval after the previous due date even when
  it was overdue (monthly keeps the day of the month, clamped), the same rule as the Notion
  task database they were imported from. Only the change into done creates it, so
  a second click never adds another; a toast says when the next one is due. Reopening the
  task deletes that next occurrence again if it's still open and untouched, so done, undone,
  done leaves exactly one. Archiving the task's project or finishing its course ends the
  recurrence (no next occurrence is made); a standalone task stops when you switch Repeats
  off in its editor. Repeating tasks never count toward a project's progress.
- **Today's picks**: the only lit rows. Their checkbox is Luna's Diamond Ring (the ring in
  the corona, the lilac bead at its edge; a tick draws inside when you finish it) and their
  day label is in the corona. Hovering the ring says why it was picked. Anything due today or tomorrow is always a pick ("Due tomorrow"). Beyond
  that, Luna chooses up to three once a day (see Today's picks below). Overdue tasks are
  already coral and are never picks.
- **Completed**: a folded section (the open state is remembered per browser) listing tasks
  finished in the last 14 days, newest first, up to 30. Unchecking one puts it back.
- **Waiting on you**: calendar invitations you haven't answered or answered "Maybe",
  from now to 30 days out (up to eight), soonest first; not tied to the shown day.
  Events you organised, or without guests, never appear. Clicking one opens it in
  Calendar (`ical://ekevent/<external id>`), where you answer; the dashboard never
  writes to your calendar.


`GET /api/day?date=YYYY-MM-DD&view=day|week|month` (defaults: today, day) returns all of it:
`timeline` and `all_day` for the shown day, `days` (one entry per day of the week or month
grid, each with a dot `count` in the week view; empty for the day view), `period` and
`has_today` (the span the view shows), `tasks`, `waiting`, `glance` (today's counts and the
next event, for the sheet), `weather`, `projects` (up to four) and `done` (Completed). Each
upcoming task carries `pick`: the reason it's a pick today, or null. `store.view_range` gives the
days a view shows (just the day, its week, or every week touching its month), so the
server asks EventKit and Luna's own events for exactly those. `Store.day_plan`
adds tasks and birthdays. Events carry `rsvp` (`pending`, `maybe`,
`accepted`, `declined` or null, from the attendee that is you) and `external_id`.
Recurring events share an id, so timed items also carry a `key` (id plus start).
Luna's own events also carry `source: "luna"`, `important` and their course's `hue`. Tasks
have no time and no estimate (v4 and v6 dropped them), and the API rejects both.

### Weather

`src/dashboard/location.py` reads where this Mac is from macOS Location Services at
town level (3 km accuracy), following you with significant-location-change monitoring.
CoreLocation reports to the run loop of the thread that made its manager, so the overlay
creates it on AppKit's main thread; a browser-only server (`python -m src.dashboard.server`)
has no location. Reading never prompts: only Settings → Calendar & location → Allow, or
Home's "Show weather", calls `POST /api/location/access` (`GET /api/location` gives the
status). Like Calendar, the prompt needs Luna.app, whose Info.plist carries the reason.

`src/dashboard/weather.py` asks Open-Meteo (free, no account or key) for the current
temperature, condition, today's high / low and chance of rain. Besides today's picks (Gemini Live, once a day), this is the dashboard's
only call to the internet: the coordinates go out rounded to one decimal (about 10 km),
nothing else about you does, and failures log only the error type. A reading is kept for
30 minutes; the server checks every 10 minutes and at once when you move or change units
(°C for metric, °F for imperial), then sends a `weather` event so Home redraws. A failed
fetch keeps the last reading and waits five minutes. WMO weather codes map to labels and
SF Symbols (day and night variants). `--demo` shows a fixed reading and never fetches.
The response covers two days back to a week ahead, hourly. `GET /api/weather` returns those
hours (`t`, `temp`, `rain` chance, `mm`, `label`, `symbol`); hovering Home's weather for a moment
(or focusing it) transforms it in place, not as an overlay: the reading fades and lifts away,
the row grows to fit, and a strip of hours (time, symbol, temperature, rain from 30%) settles
in, starting on the current hour where the icon was. Drag it, or scroll, to move through the
days; each midnight names its day ("Mon 16"), past hours are dimmed and the right edge fades
out. Leaving fades the reading back. The hours load with Home, so there's no wait on hover. When the place changes, the rounded
coordinates are saved as the `weather_place` setting so Luna's `get_weather` tool, which
runs in the notch app's process and has no location of its own, can use the same spot.
WeatherKit was ruled out: it needs a paid developer account and provisioned signing.

### Today's picks

`src/dashboard/picks.py` chooses, once a day, which upcoming tasks to highlight beyond the
ones due by tomorrow. The server checks every minute; when the date has changed (just after
midnight, or on waking or launching later) it opens one Gemini Live session
(`gemini-3.8-live`, the voice model) rather than a Flash text model, because Live has its
own quota and the notch chat needs Flash's. The session gets a JSON list of the candidates
(upcoming tasks not due by tomorrow: title, due date, days left, course, project and its
category, priority, whether it's started, repeat, and the description as plain words up
to 600 characters), what's already chosen, and
today's event times without titles. It's told to weigh the work a task needs against the
days left (start assignments early; leave short routines like a weekly prep until they're
close) and to answer by calling `pick_tasks` once with at most three ids and a reason of
ten words or fewer. Audio is never played or kept. Ids not in the list are dropped.

The result is stored in the `task_picks` setting with the date, so it lasts the day and a
relaunch doesn't ask again; tasks added later that day are picked only if due by tomorrow.
A failure (no tool call, timeout, network) logs only the error type and retries after 15
minutes, three times a day. Without `GOOGLE_API_KEY` in Luna's `.env` there are no
model picks, only the due-by-tomorrow ones. `--demo` uses a fixed pick and never connects.
Tests use the fake Live server from `tests/test_mcp_pipeline.py`.

### Events

Luna keeps its own events (the `events` table): a name, start and end (or all day),
a short note, a place, an optional course, an **important** flag and guests with their
answers. Home shows them with whatever macOS Calendar still has. Blocks are quiet,
tinted with their course's hue; an important event stands out in every view (a
stronger fill in its hue, a star); any other only when it's the next or current one
(the corona edge). Clicking one opens it in Home's sheet: your own events open ready to
edit (course, importance, date and times or all day, place, note, guests by name or
email); an invitation opens to answer (who invited you, Going / Maybe / Can't go, and
the other guests' answers). `+` in the calendar head adds one on the shown day.
Invitations still waiting appear under "Waiting on you". Receiving invitations by email
(iCalendar), sending them, and importing `.ics` files are not built yet.

### Calendar

macOS only asks for Calendar access on behalf of an app that says why it wants it, so
a Python started from a shell is refused without a prompt. `scripts/build_app.py`
builds `build/Luna.app` (gitignored, never installed anywhere): a small native executable,
`dev.luna`, that runs this checkout's `.venv` Python inside its own
process (embedded), with every reason in its Info.plist. So macOS attributes the
microphone, Calendar, Location, Messages automation, Documents-folder and Full Disk Access
prompts to "Luna". Output goes to `~/Library/Logs/Luna/luna.log`. It's signed with a
Developer ID or Apple Development certificate when the keychain has a valid one
(`LUNA_SIGN_IDENTITY` picks one), which keeps the grants across rebuilds; otherwise
ad-hoc, and macOS asks again after each rebuild.

`src/dashboard/calendar.py` reads macOS Calendar through EventKit, read-only, so
every account synced to this Mac (Google, iCloud, Exchange) shows up. Reading the
day never prompts: until access is granted the timeline shows tasks only, with a
"Connect Calendar" button. That button, or Settings → Calendar → Allow, calls
`POST /api/calendar/access`, which shows macOS's prompt once. If access is off,
the button opens the Calendars privacy pane. `--demo` uses a fixed sample week.

## Data, server and safety

- Invoicing was removed for v1. Databases created before that may still hold `clients`
  and `invoices` tables; nothing reads or changes them.
- One SQLite file, `~/Library/Application Support/Luna/dashboard.sqlite3` (0600, WAL,
  schema version 9). Every writable column is validated; unknown fields are rejected.
  v9 adds `announcements` (per course, from Quercus) and `tasks.external_state`.
  v8 gives tasks an `external_id` (unique; `canvas:<course>:<assignment>` for Quercus tasks).
  v7 lets a project wear its own colour and icon (empty means its category's).
  v6 replaced project kind/status/progress/due with a category (plus an optional label)
  and an archived flag, gave tasks a status (not started, in progress, done), a Markdown
  description and an optional course (dropping area and estimate), added courses, notes
  and events, and dropped project items. Opening an older file migrates it in place:
  kinds map to categories (design, video and research become "other" with that label),
  archived and shipped projects become archived, and task areas that look like course
  codes (CSC311) become courses that keep their tasks.
- The loopback aiohttp server runs on a random port with a per-launch token that
  becomes an HttpOnly, SameSite=Strict cookie. It accepts loopback Host headers
  only, refuses cross-origin writes and sends `no-cache` for static files.
  `/api/file` serves only project folders and items. `/api/messages/attachment`
  serves only `~/Library/Messages/Attachments`. `/api/wallpaper` serves only the
  current desktop picture.
- SF Symbols are rendered by AppKit into white PNG masks (`/sym/<name>`) so the
  page uses the system glyphs.

## Messages

iMessage and SMS are read from `~/Library/Messages/chat.db` (read-only; needs Full Disk
Access) and sent through Messages with `osascript`; the text and chat ID travel as
arguments. Instagram, LinkedIn, Discord and others come through the
[Beeper Desktop API](https://developers.beeper.com/desktop-api), served by Beeper Server
(headless, `127.0.0.1:23374`, tried first) or Beeper Desktop (`127.0.0.1:23373`); the
first that answers `/v1/info` is used. The token is kept in the Keychain
(`Luna Dashboard` / `beeper`; `Luna Dashboard (dev)` for a development build). With Beeper Server the token is the one `beeper setup`
saves, since the server has no screen to approve an OAuth request. Both are the
apps' live state, polled every 2 s, so replies from the original apps appear here.
Failures surface as reason codes, never provider text.

## Projects, courses and notes

- **Projects**: a name, a category, a description, a folder, pinned, archived. No date:
  due dates belong to tasks. Folders are scanned read-only: media by type, DAW sessions,
  git state, languages and word counts. "Latest mix" skips `Stems/`, `Samples/`,
  `Recorded/` and similar folders.
- **Courses**: a code (the only name; the title goes in the description), a description,
  a folder, a colour, an icon, and active or finished. A new course's colour is the hue
  the fewest active courses use (ties settle on the code), and its icon follows the
  subject letters of its code (CSC → cpu, MAT → function, PHY → atom), falling back to a
  book; both update as you type the code until you pick one. It's computed locally
  (`store.course_defaults`, mirrored in `web/js/work.js`), so it's instant.
- A project's colour and icon follow its category until you choose your own (Edit, or the
  swatches at the top of its right-click menu).
- **Right-click** a project or course in the rail for a quick colour, Edit…, Pin (projects),
  Archive or Mark as finished, Show in Finder and Delete; a note for Show in Finder and Delete.
- **Read and Edit.** A document opens in Read mode, or in Edit when it's new or empty, and
  changes mode only when you ask: ⌘E, or the pencil/book button in its bar. Clicking or
  moving the cursor never changes how anything looks. Edit mode is also writing mode: the
  rail and the right column slide off the edges and the page widens (one 600 ms view
  transition) to 1040 px of text; pointing at the window's left or right edge brings that
  panel back over the page, and moving off it lets it go. The line at the top of the page
  stays where it was through the switch. Leaving the page returns to Read. The work pages
  fit the window: lists scroll inside their panes, never the page.
- **Tasks** belong to a project, a course or nothing. Their description is Markdown.
- **Notes** are Markdown files: `<folder>/Notes/<title>.md` for a project or course with a
  folder, else `<data>/notes/<kind>-<id>/`. Renaming a note renames its file; deleting it
  moves the file to the Trash. Images and files dropped or pasted into a note or task
  description go to `assets/` beside the notes (`<data>/tasks/assets/` for tasks that
  belong to nothing) and are linked relatively (`![](assets/plot.png)`), served by
  `/api/media` only from inside that folder. `--demo` keeps notes in a temp folder.
- **The editor** (`src/dashboard/editor/editor.js`, bundled into `web/vendor/editor.js`) is
  CodeMirror 6 with two modes. **Read** renders everything and isn't editable: Markdown
  marks hide, `$…$` and `$$…$$` render with KaTeX, `::: columns … +++ … :::` sits side by
  side, images show in place, `> [!note] Title` is a quote-style callout; task boxes still
  click (and save). **Edit** is the source, styled (headings sized, marks dimmed), and nothing
  in it depends on where the cursor is: code, callouts and display maths keep a frame around
  their source, every display equation keeps its rendering underneath (updating as you
  type), images show under the line that names them, and inline maths under the cursor
  previews in a tip floating above the line. Notes and task descriptions share it; only
  the header differs (a task's status, due date, priority and repeat; a note's date).
  - **Callouts** are containers, the syntax the notes were written in: `::: kind Title` …
    `:::`. The body is full Markdown (maths, lists, quotes, code). Kinds: `note` (sky), `tip`
    (lagoon), `definition` (violet), `theorem` (orchid), `example` (sand) and `warning`
    (amber). The header names the kind before the title ("Definition  Bootstrap
    Aggregation") so colour is never the only cue; a warning shows ⚠️ instead of a name.
    A title that starts with its own label ("Theorem: Euler's Formula" under `definition`)
    shows as that kind. Unknown kinds read as notes under their own name. In Edit mode its
    source is framed in the same box.
  - **Code blocks** render as a panel with the language's name, a copy button and
    highlighting, in the palette; inside, the same parser highlights the source as you
    type. Fence names and their aliases: Python (`py`), JavaScript/TypeScript (`js`, `ts`,
    `jsx`, `tsx`), C/C++ (`c`, `cpp`, `h`), SQL (PostgreSQL; `postgres`, `psql`, `sqlite`…),
    Java, Rust, Go, HTML, CSS, JSON, Shell (`sh`, `bash`, `zsh`), Haskell, LaTeX and YAML.
    Other names show unhighlighted under their own label. `code.js` holds the list.
  - **`/` menu**: headings, lists, to-dos, quote, definition, theorem, example, tip, note,
    warning, block and inline equations, columns, code (the caret lands where the language
    goes), table, divider, image and file.
  - **`$$…$$` inside a line** renders as display maths in its own place, the same way it
    reads inside callouts and columns.
  - **Paragraph spacing**: a blank line between paragraphs is a 12 px paragraph gap, not a
    line of text, in both modes (code keeps its blank lines). In Read mode a run of blank
    lines is still one gap, as Markdown reads it as one break; in Edit each blank line keeps
    its gap so the cursor always has a line to sit on, and a blank line grows to a full line
    as you type into it. Callouts and columns space their paragraphs the same way, and their
    headings use the editor's own sizes.
  - **Formatting keys** (Edit mode): ⌘B bold, ⌘I italic, ⌘⇧C code, ⌘⇧S strikethrough,
    ⌘⇧E inline maths (each toggles), ⌘K link. ⌘E is taken by Read/Edit, as in Obsidian. Typing `*` `_` `` ` `` `~` `$` or `=` over a
    selection wraps it; brackets pair as you type (quotes don't).
  - **LaTeX completion**: a backslash inside maths offers common commands, each shown
    rendered; templates like `\frac{}{}` step through their fields with Tab and out past
    the last brace.
  - **Off-screen blocks** report the height they last drew at (or a guess from their
    source), so long notes don't grow as you scroll and the scrollbar stays put.
  - **Delimiters can't run away**: typing the third backtick of an opening fence adds the
    closing one (unless it would pair with an unmatched fence below). `$$` only pairs up to
    a blank line or another block, and `:::` containers nest, so a stray opener no longer
    swallows the blocks below it.
  - Rendered blocks get their spacing from padding on a wrapper (`.md-block`), never
    margins: CodeMirror measures widgets without margins, so a margin shifts every line
    below and sends clicks and ↑/↓ to the wrong line. The editor also re-measures when the
    page's scroll container scrolls, whenever its own size changes (CodeMirror ignores
    resizes within 75 ms of an update, which is exactly when a mode switch widens the page)
    and when fonts finish loading; the page waits for KaTeX's stylesheet before the first
    render.
  - After changing it: `cd src/dashboard/editor && npm install && npm run build` (the bundle
    and KaTeX's fonts are committed).

Apple Health exports and bank CSVs import as before (`src/dashboard/imports.py`).

### Quercus

The dashboard keeps every active course in step with Quercus by itself: at launch, every
15 minutes, and when you press refresh beside a course's Announcements
(`POST /api/quercus/sync`; `uv run python -m scripts.sync_canvas` does the same from a
shell). It's deterministic code (`src/dashboard/canvas.py`) calling the read-only Canvas
MCP integration (`src/install/canvas.py`, from setup or `scripts/setup_canvas.py`) like an API; no model is involved, and
Canvas is never written to. The last result is the `quercus_sync` setting.

- **Which courses.** Canvas courses match Luna courses by code (`CSC311H1F 20269` →
  `CSC311`); only active Luna courses sync, and when a code repeats (a retake) the
  newest Canvas course wins. Finished courses are left alone.
- **Tasks.** Assignments (graded quizzes and New Quizzes are assignments in Canvas) and
  Classic quizzes that have no assignment (practice quizzes, surveys) become course tasks
  due on the Toronto day of their deadline. Past or already-submitted items are never
  created. A rerun moves the due date of open tasks whose deadline changed. When Canvas
  reports an item submitted, graded or excused, its task is checked off once, at that
  change (`tasks.external_state`); reopen it and it stays open. Titles, descriptions and
  status are otherwise yours. A hand-made task for the same item (same name, or the same
  short form, `A3` = `Assignment 3`, due within three days) is adopted, not duplicated.
  An unknown submission format, or two assignments with one name, never completes anything.
- **Announcements.** The last 150 days are mirrored into the course's `announcements`:
  title, author, plain-text body (Canvas's 2,000-character preview; a link opens the
  rest), link and time. New ones arrive read or unread as they are on Quercus; after that
  the read state is yours (`PATCH /api/announcements/{id}`). Edits update in place;
  ones deleted on Quercus disappear. The course page lists the newest five (all on
  request); opening one marks it read.
- Names and bodies are instructor text: stored and shown, never acted on.

## Not yet

- Side navigation and pages for body, journal, money and people.
- Calendar invitations by email (iMIP), sending invitations, `.ics` import, and repeating
  events. macOS Calendar is still read alongside Luna's own events.
- No Luna integration.
