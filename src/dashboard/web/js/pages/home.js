// Home: a sheet and a desk. The sheet (left) is you and today: the date, a greeting, the weather, what's next, tasks
// due within a week and invitations waiting on an answer. The desk (right) is work: the projects to pick up, each kind
// showing what you'd reach for next, and the calendar (day, week or month). Tasks are placed only by their due date, so
// they never go on the calendar. Luna's corona appears only as light: the now line, today's dots and the next event.

import { h, s, api, fmt, go, settings, isoDay, addDays } from "../core.js";
import { icon, pane, iconButton, button, check, empty, field, input, select, segmented, formData, openLayer, sheet, fail, toast } from "../ui.js";
import { onPlayer, fileUrl } from "../player.js";
import { waveform, playButton } from "./projects.js";
import { CATEGORIES, PRIORITIES, REPEATS as REPEAT_OPTIONS, hue, categorySymbol, tile, statusButton } from "../work.js";
import { loadEvent, eventPanel } from "../event.js";

const VIEWS = [{ value: "day", label: "Day" }, { value: "week", label: "Week" }, { value: "month", label: "Month" }];
const VIEW_KEY = "luna.home.view";
const savedView = (() => { try { return localStorage.getItem(VIEW_KEY); } catch { return null; } })();
const ui = { day: null, view: VIEWS.some((v) => v.value === savedView) ? savedView : "day" };  // day null = today
let rerender = null, shown = null;
const onHome = () => location.hash === "" || location.hash.startsWith("#/home");
setInterval(() => { if (rerender && onHome()) rerender(); }, 30000);
onPlayer(() => rerender && onHome() && rerender());

const url = () => `/api/day?view=${ui.view}${ui.day ? `&date=${ui.day}` : ""}`;
// The grid holds all 24 hours but is sized to the hours that matter: an hour of air around the shown events (and now,
// today), at least eight hours, fitted to the space Home gives it; anything else is a scroll away.
const MIN_SPAN = 8 * 60, HOUR_MIN = 28, HOUR_MAX = 72;
const hour24 = new Intl.DateTimeFormat(undefined, { hour: "numeric" }).resolvedOptions().hourCycle?.startsWith("h2");
const toMin = (hhmm) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5));
const atMin = (m) => { const d = new Date(); d.setHours(Math.floor(m / 60), m % 60, 0, 0); return d; };
// "1:30", or "13:30" where the system uses a 24-hour clock.
const short = (hhmm) => fmt.time(atMin(toMin(hhmm))).replace(/\s?[AP]M$/i, "");
const hourLabel = (hr) => {
  if (hour24) return String(hr).padStart(2, "0");
  return `${hr % 12 || 12} ${hr < 12 || hr === 24 ? "AM" : "PM"}`;  // every label: the grid can open anywhere in the day
};
// "7:30 PM", "11 PM" (or "19:30", "23"): month cells have no room for ":00" but must not lose AM/PM.
const compact = (hhmm) => atMin(toMin(hhmm)).toLocaleTimeString(undefined, { hour: "numeric", minute: hhmm.endsWith(":00") ? undefined : "2-digit" });
const nowMin = () => { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); };
const parseDay = (iso) => new Date(`${iso}T12:00:00`);
const y = (m) => `${(m / 1440) * 100}%`;

let params = [];
async function load(p = []) {
  params = p;
  const [plan, event] = await Promise.all([api.get(url()), loadEvent(p)]);
  return { ...plan, panel: event };
}

// Where a task opens: inside its project or course, or on its own page.
export const taskLink = (t) => (t.owner ? `${t.owner.kind === "project" ? "projects" : "academia"}/${t.owner.id}/task/${t.id}` : `task/${t.id}`);

function show(view, day, ctx) {
  ui.view = view;
  if (day !== undefined) ui.day = day === isoDay() ? null : day;
  try { localStorage.setItem(VIEW_KEY, view); } catch { /* the view just isn't remembered */ }
  ctx.reloadAnimated("cal-fade");
}

const repeatLabel = (r) => (r ? (REPEAT_OPTIONS.find(([v]) => v === r) || [null, null])[1] : null);  // nothing for one-off tasks

// A repeating task comes back as its next occurrence; say when, so it doesn't look like it vanished.
async function toggle(task, done) {
  // Finishing a task lets its row fold away first (the tick draws, then the row collapses), then it's saved.
  const row = done && document.querySelector(`.home-sheet .row[data-key="${task.id}"]`);
  if (row && !matchMedia("(prefers-reduced-motion: reduce)").matches) { row.classList.add("is-completing"); await new Promise((r) => setTimeout(r, 680)); }
  try {
    const row = await api.post(`/api/tasks/${task.id}/complete`, { done });
    if (done) toast(row.next ? `Done. Next one is due ${fmt.relativeLower(row.next.due)}` : "Done");
    else toast(row.removed ? "Back on your list. Removed the next one it had added" : "Back on your list");
  } catch (e) { fail(e); }
}

// Calendar head -------------------------------------------------------------------------------------------------
// Inside the calendar pane: what it shows, then ‹ Today › and the view switch. The arrows step the view itself (a day,
// a week, a month); the middle button returns to the period holding today and says which ("This Week").

const STEP = { day: ["day", "Today"], week: ["week", "This Week"], month: ["month", "This Month"] };
const events = (x) => x.timeline.length + x.all_day.filter((a) => a.kind === "event").length;
const range = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });

function heading(d) {
  const [a, b] = d.period.map(parseDay);
  const shown = d.view === "day" ? events(d) : d.days.filter((x) => x.date >= d.period[0] && x.date <= d.period[1]).reduce((n, x) => n + events(x), 0);
  const count = shown ? fmt.plural(shown, "event") : "No events";
  if (d.view === "week") return [range.formatRange(a, b), `${b.getFullYear()} · ${count}`];
  if (d.view === "month") return [a.toLocaleDateString(undefined, { month: "long", year: "numeric" }), count];
  return [a.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" }), count];
}

function page(d, n, ctx) {
  const date = parseDay(d.date);
  let next;
  if (d.view === "month") {  // same day of the month, clamped: Jan 31 → Feb 28
    const last = new Date(date.getFullYear(), date.getMonth() + n + 1, 0).getDate();
    next = new Date(date.getFullYear(), date.getMonth() + n, Math.min(date.getDate(), last));
  } else next = addDays(date, d.view === "week" ? 7 * n : n);
  ui.day = isoDay(next) === isoDay() ? null : isoDay(next);
  ctx.reloadAnimated(n > 0 ? "cal-next" : "cal-prev");
}

function calendarHead(d, ctx) {
  const [title, sub] = heading(d), [unit, label] = STEP[d.view];
  return h("header", { class: "cal-head" },
    h("div", { class: "cal-title" }, h("h2", null, title), h("span", { class: "sec-sub" }, sub)),
    h("span", { class: "grow" }),
    calendarControl(d, ctx),
    h("div", { class: "week-nav" },
      iconButton("chevron.left", { title: `Previous ${unit}`, onClick: () => page(d, -1, ctx) }),
      button(label, { kind: "ghost", small: true, class: "today-btn", disabled: d.has_today, title: d.has_today ? null : `Go to ${label.toLowerCase()}`,
        onClick: () => { ui.day = null; ctx.reload(); } }),
      iconButton("chevron.right", { title: `Next ${unit}`, onClick: () => page(d, 1, ctx) })),
    segmented("home-view", VIEWS, d.view, (v) => show(v, undefined, ctx)),
    iconButton("plus", { title: "New event", onClick: () => go(`home/event/new?date=${d.date}`) }));
}

// Calendar -----------------------------------------------------------------------------------------------------

// Overlapping items share the width: each takes the first free lane in its cluster.
function lanes(items) {
  const out = [];
  let cluster = [], end = -1;
  const flush = () => { const n = Math.max(1, ...cluster.map((c) => c.lane + 1)); cluster.forEach((c) => { c.lanes = n; }); cluster = []; };
  for (const item of items) {
    const a = toMin(item.start), b = Math.max(a + 15, toMin(item.end));
    if (a >= end) { flush(); end = -1; }
    const taken = new Set(cluster.filter((c) => c.b > a).map((c) => c.lane));
    let lane = 0; while (taken.has(lane)) lane++;
    const placed = { ...item, a, b, lane };
    cluster.push(placed); out.push(placed); end = Math.max(end, b);
  }
  flush();
  return out;
}

// Elapsed and next are worked out here, not by the server, so the 30-second refresh keeps them current.
function placed(day, items) {
  const today = isoDay(), now = nowMin(), list = lanes(items);
  const next = day === today ? list.find((i) => i.b > now) : null;
  return list.map((i) => ({ ...i, past: day < today || (day === today && i.b <= now), isNext: next != null && i.key === next.key,
    until: day === today && i.b > now ? (i.a <= now ? "Now" : `in ${fmt.minutes(i.a - now)}`) : null }));
}

// Events are quiet, tinted with their course's hue. Important ones stand out on every view; any other only when it's
// the next or current one. Luna's own events open in the sheet; ones from macOS Calendar open there.
function eventBlock(i, style, compact) {
  const own = i.source === "luna";
  return h("div", { key: i.key, class: ["blk blk-event", compact && "is-compact", i.past && "is-past", i.isNext && "is-next", i.important && "is-important",
    own && "is-own", params[1] && params[1].split("?")[0] === i.id && "is-selected"],
  style: { ...style, ...(i.hue ? hue(i.hue) : {}) }, role: "button", tabindex: 0,
  "data-tip": own ? null : i.calendar ? `${i.calendar} calendar` : null,
  onClick: () => (own ? go(`home/event/${i.id}`) : openEvent(i)) },
  h("div", { class: "blk-main" }, h("span", { class: "blk-title" }, i.important ? icon("star.fill", { class: "blk-star" }) : null, i.title),
    h("span", { class: "blk-sub" }, `${short(i.start)} – ${short(i.end)}${i.meta ? ` · ${i.meta}` : ""}`)),
  // How far off it is, shown on hover only.
  i.until && h("span", { class: "blk-until" }, i.until));
}

function hourLines() {
  const out = [];
  for (let hr = 0; hr <= 24; hr++) out.push(h("div", { key: `h${hr}`, class: "hr", style: { top: y(hr * 60) } }, hr < 24 ? h("span", null, hourLabel(hr)) : null));
  return out;
}

// Where you are in the day, marked by Luna's Diamond Ring; no clock. The line runs under the blocks.
function nowLine() {
  const m = nowMin();
  return [
    h("div", { key: "now", class: "now-line", style: { top: y(m) }, "aria-hidden": "true" }),
    s("svg", { key: "now-mark", class: "now-mark", viewBox: "0 0 16 16", style: { top: y(m) }, "aria-hidden": "true" },
      s("circle", { cx: 8, cy: 8, r: 5 }), s("circle", { class: "bead", cx: 11.54, cy: 4.46, r: 1.9 })),
  ];
}

function visibleHours(days) {
  const today = isoDay(), timed = days.flatMap((x) => x.timeline);
  let a = Math.min(...timed.map((i) => toMin(i.start))), b = Math.max(...timed.map((i) => Math.max(toMin(i.end), toMin(i.start) + 15)));
  if (days.some((x) => x.date === today)) {
    const n = nowMin(), withNow = [Math.min(a, n - 30), Math.max(b, n + 30)];
    if (!timed.length || withNow[1] - withNow[0] <= 16 * 60) [a, b] = withNow;  // a 2 AM glance doesn't stretch a day of 2 PM events
  }
  if (!Number.isFinite(a)) [a, b] = [9 * 60, 18 * 60];
  a = Math.max(0, Math.floor((a - 60) / 60) * 60);
  b = Math.min(1440, Math.ceil((b + 60) / 60) * 60);
  if (b - a < MIN_SPAN) { const pad = (MIN_SPAN - (b - a)) / 2; a = Math.max(0, Math.min(a - pad, 1440 - MIN_SPAN)); b = a + MIN_SPAN; }
  return [a, b];
}

// Sets the hour height on every render (and resize) so the window fills the space; scrolls to it once per day and view.
function fit(d, days) {
  const key = `${d.view}:${d.date}`, [a, b] = visibleHours(days);
  return (el) => {
    // Resizing keeps the same time at the top, whether it came from the fit or from your own scrolling.
    const size = () => {
      const old = parseFloat(el.style.getPropertyValue("--hour")) || 0;
      const hour = Math.max(HOUR_MIN, Math.min(HOUR_MAX, (el.clientHeight - 20) / ((b - a) / 60)));
      if (hour === old) return hour;
      const top = old ? el.scrollTop / old : null;
      el.style.setProperty("--hour", `${hour}px`);
      if (top != null) el.scrollTop = top * hour;
      return hour;
    };
    // The overlay can render while hidden, when the grid has no height yet: the first scroll waits for a real size.
    const settle = () => {
      const hour = size();
      if (el.__pending != null && el.clientHeight > 40) { el.scrollTop = (el.__pending / 60) * hour; el.__pending = null; }
    };
    el.__fit = settle;
    if (!el.__observer) { el.__observer = new ResizeObserver(() => el.__fit && el.__fit()); el.__observer.observe(el); }
    if (el.__shown !== key) { el.__shown = key; el.__pending = a; }
    settle();
  };
}

function allDayItem(a) {
  return h("span", { key: `${a.kind}-${a.id}`, class: "all-day-item" }, a.title);
}

function dayView(d) {
  const items = placed(d.date, d.timeline);
  const block = (i) => eventBlock(i, { top: y(i.a), height: `calc(${((i.b - i.a) / 1440) * 100}% - 2px)`,
    left: `calc(56px + (100% - 68px) * ${i.lane / i.lanes})`, width: `calc((100% - 68px) / ${i.lanes} - ${i.lanes > 1 ? 4 : 0}px)` }, i.b - i.a < 45);
  return [
    d.all_day.length ? h("div", { key: "all-day", class: "all-day" }, h("span", { class: "all-day-label" }, "All day"), d.all_day.map(allDayItem)) : null,
    h("div", { key: "scroll-day", class: "cal-scroll", ref: fit(d, [{ date: d.date, timeline: d.timeline }]) },
      h("div", { class: "rail" }, hourLines(), d.today && nowLine(), items.map(block))),
  ];
}

// The week's days, over the columns they name: today white, one dot per event, task due or birthday (up to three).
function weekDays(d, ctx) {
  const today = isoDay();
  return h("div", { key: "wk-head", class: "wk-row wk-head", role: "group", "aria-label": "Days" }, d.days.map((x) => h("button", {
    type: "button", key: x.date, class: ["wd", x.date === today && "is-today"], "aria-current": x.date === today ? "date" : null,
    title: "Open this day", onClick: () => show("day", x.date, ctx) },
  // A month starting mid-week names itself on its 1st.
  h("small", null, x.date === today ? "Today" : parseDay(x.date).toLocaleDateString(undefined, x.date.endsWith("-01") && x !== d.days[0]
    ? { month: "short" } : { weekday: "short" })),
  h("b", null, String(parseDay(x.date).getDate())),
  h("span", { class: "pips", "data-tip": x.count ? `${fmt.plural(x.count, "thing")} planned` : null },
    Array.from({ length: Math.min(3, x.count) }, (_, n) => h("i", { key: String(n) }))))));
}

function weekView(d, ctx) {
  const today = isoDay();
  return [
    weekDays(d, ctx),
    d.days.some((x) => x.all_day.length) ? h("div", { key: "wk-all", class: "wk-row wk-all" },
      d.days.map((x) => h("div", { key: x.date, class: "wk-all-col" }, x.all_day.map(allDayItem)))) : null,
    h("div", { key: "scroll-week", class: "cal-scroll", ref: fit(d, d.days) },
      h("div", { class: "rail wk-grid" }, hourLines(),
        h("div", { class: "wk-cols" }, d.days.map((x) => h("div", { key: x.date, class: ["wk-col", x.date === today && "is-today"] },
          x.date === today && nowLine(),
          placed(x.date, x.timeline).map((i) => eventBlock(i, { top: y(i.a), height: `calc(${((i.b - i.a) / 1440) * 100}% - 2px)`,
            left: `calc(100% * ${i.lane / i.lanes} + 2px)`, width: `calc(100% / ${i.lanes} - 4px)` }, true))))))),
  ];
}

function monthView(d, ctx) {
  const today = isoDay(), month = d.date.slice(0, 7);
  const names = d.days.slice(0, 7).map((x) => parseDay(x.date).toLocaleDateString(undefined, { weekday: "short" }));
  return [
    h("div", { key: "mo-head", class: "mo-head" }, names.map((n) => h("span", { key: n }, n))),
    h("div", { key: "mo-grid", class: "mo-grid", style: { "grid-template-rows": `repeat(${d.days.length / 7}, minmax(0, 1fr))` } },
      d.days.map((x) => {
        const items = [...x.all_day.map((a) => ({ key: `${a.kind}-${a.id}`, title: a.title, allDay: true })),
          ...x.timeline.map((i) => ({ key: i.key, title: i.title, time: compact(i.start) }))];
        const shownItems = items.length > 3 ? items.slice(0, 2) : items;
        return h("button", { type: "button", key: x.date, title: "Open this day", onClick: () => show("day", x.date, ctx),
          class: ["mo-cell", x.date.slice(0, 7) !== month && "is-out", x.date === today && "is-today"] },
        h("b", null, String(parseDay(x.date).getDate())),
        shownItems.map((i) => h("span", { key: i.key, class: ["mo-item", i.allDay && "is-all-day"] }, i.time && h("i", null, i.time), i.title)),
        items.length > shownItems.length && h("span", { class: "mo-more" }, `${items.length - shownItems.length} more`));
      })),
  ];
}

function calendar(d, ctx) {
  return pane("home-calendar", { class: ["home-cal", `is-${d.view}`], key: "home-calendar" }, calendarHead(d, ctx),
    d.view === "week" ? weekView(d, ctx) : d.view === "month" ? monthView(d, ctx) : dayView(d));
}

function calendarControl(d, ctx) {
  if (d.calendar === "not_determined") {
    return button("Connect Calendar", { kind: "ghost", small: true, icon: "calendar", onClick: async () => {
      try { const r = await api.post("/api/calendar/access", {}); if (r.status !== "ready") toast("Calendar access is off. You can turn it on in System Settings.", "bad"); ctx.reload(); } catch (e) { fail(e); }
    } });
  }
  if (d.calendar === "denied" || d.calendar === "write_only") {
    return button("Calendar access is off", { kind: "ghost", small: true, icon: "calendar.badge.exclamationmark",
      onClick: () => api.post("/api/open", { url: "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars" }).catch(fail) });
  }
  return null;
}

async function addTask(d) {
  const owners = await api.get("/api/owners").catch(() => ({ projects: [], courses: [] }));
  const choices = [["", "Nothing (a standalone task)"], ...owners.courses.map((c) => [`course:${c.id}`, c.code]),
    ...owners.projects.map((p) => [`project:${p.id}`, p.title])];
  openLayer((close) => sheet("Add a task", close,
    h("div", { class: "form-grid" },
      field("Task", input({ name: "title", autofocus: true, placeholder: "Send stems to Theo" }), { class: "span-2" }),
      field("Due", input({ name: "due", type: "date", value: d.date, required: true })),
      field("Priority", select("priority", PRIORITIES, 0)),
      field("For", select("owner", choices, ""), { class: "span-2" }),
      field("Repeats", select("repeat", [["", "Never"], ...REPEAT_OPTIONS], ""), { hint: "Finishing it adds the next one", class: "span-2" })),
  [h("span", { class: "grow" }), button("Cancel", { onClick: () => close() }), button("Add", { kind: "primary", type: "submit" })],
  { onSubmit: async (form) => {
    const data = formData(form);
    if (!data.title || !data.due) return;
    const [kind, id] = (data.owner || "").split(":");
    try {
      await api.post("/api/tasks", { title: data.title, due: data.due, priority: Number(data.priority) || 0, repeat: data.repeat || "",
        project_id: kind === "project" ? id : null, course_id: kind === "course" ? id : null });
      close();
    } catch (e) { fail(e); }
  } }), { width: 460 });
}

// Side column ---------------------------------------------------------------------------------------------------

// "Tue 17": within a week or so the weekday alone is unambiguous; the date anchors it.
const shortDay = (iso) => iso === isoDay() ? "Today" : `${parseDay(iso).toLocaleDateString(undefined, { weekday: "short" })} ${parseDay(iso).getDate()}`;
const longDay = (iso) => parseDay(iso).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
// Calendar opens the event itself, where you answer it. Without an id, Calendar just opens.
const openEvent = (e) => api.post("/api/open", { url: e.external_id ? `ical://ekevent/${encodeURIComponent(e.external_id)}?method=show&options=more` : "ical://" }).catch(fail);

// Luna's Diamond Ring is the checkbox of a task Luna picked for today: the ring in the corona, the bead at its edge,
// a tick drawn inside when it's done. Hovering it says why it was picked.
function pickCheck(t) {
  return h("button", { type: "button", class: "check pick-check", "data-tip": t.pick, "aria-label": `Mark ${t.title} as done. Luna's pick: ${t.pick}`,
    onClick: (e) => { e.stopPropagation(); e.currentTarget.classList.add("is-done"); toggle(t, true); } },
  s("svg", { viewBox: "0 0 20 20", width: 18, height: 18, "aria-hidden": "true" },
    s("circle", { cx: 10, cy: 10, r: 7.2, class: "pick-ring" }), s("circle", { cx: 15.1, cy: 4.9, r: 2.1, class: "pick-bead" }),
    s("path", { d: "M6.6 10.3 8.9 12.6 13.4 8", class: "check-tick" })));
}

// Overdue first, in coral; then what's coming. Only Luna's picks for today (due by tomorrow, or chosen by Luna each
// midnight) are lit: their checkbox is Luna's mark and their day is in the corona. Everything else stays neutral.
// A task in progress shows the half-filled ring it has everywhere else.
function taskRows(list, late) {
  return list.map((t, n) => h("div", { key: t.id, class: ["row", late ? "is-late" : "is-upcoming", t.pick && !late && "is-pick"] },
    h("span", { class: "row-time", "data-tip": `Due ${fmt.relativeLower(t.due)}` }, n && list[n - 1].due === t.due ? "" : shortDay(t.due)),
    h("span", { class: "side-check" }, t.pick && !late ? pickCheck(t) : t.status === "doing" ? statusButton(t)
      : check(false, () => toggle(t, true), { label: `Mark ${t.title} as done` })),
    h("a", { class: "row-main", href: `#/${taskLink(t)}` }, h("div", { class: "row-title" }, t.title),
      h("div", { class: "row-sub" }, late ? h("span", null, `Due ${fmt.relativeLower(t.due)}`) : null,
        t.owner ? h("span", { class: "row-owner", style: hue(t.owner.hue) }, t.owner.label) : null,
        t.status === "doing" ? h("span", null, "In progress") : null,
        repeatLabel(t.repeat) ? h("span", null, repeatLabel(t.repeat)) : null))));
}

// Completed: the last two weeks, folded away by default. Unchecking puts a task back (and takes back the next
// occurrence a repeating task added, while it's untouched).
const DONE_KEY = "luna.home.done";
let doneOpen = (() => { try { return localStorage.getItem(DONE_KEY) === "1"; } catch { return false; } })();

function doneSection(d, ctx) {
  if (!d.done || !d.done.length) return null;
  const flip = () => { doneOpen = !doneOpen; try { localStorage.setItem(DONE_KEY, doneOpen ? "1" : "0"); } catch { /* not remembered */ } ctx.refresh(); };
  return h("section", { key: "done", class: ["tasks-done", doneOpen && "is-open"], "aria-label": "Completed tasks" },
    h("header", { class: "sec-head" },
      h("button", { type: "button", class: "disclose", "aria-expanded": String(doneOpen), onClick: flip },
        icon("chevron.right", { class: "disclose-chev" }), h("h2", null, "Completed"), h("span", { class: "sec-sub" }, String(d.done.length)))),
    doneOpen ? d.done.map((t) => h("div", { key: t.id, class: "row is-done" },
      h("span", { class: "row-time", "data-tip": `Finished ${fmt.since(t.done_at)}` }, shortDay(isoDay(new Date(t.done_at * 1000)))),
      h("span", { class: "side-check" }, check(true, () => toggle(t, false), { label: `Mark ${t.title} as not done` })),
      h("a", { class: "row-main", href: `#/${taskLink(t)}` }, h("div", { class: "row-title" }, t.title),
        h("div", { class: "row-sub" }, t.due ? h("span", null, `Was due ${fmt.relativeLower(t.due)}`) : null,
          t.owner ? h("span", { class: "row-owner", style: hue(t.owner.hue) }, t.owner.label) : null)))) : null);
}

function taskSection(d) {
  const late = d.tasks.filter((t) => t.late), upcoming = d.tasks.filter((t) => !t.late);
  return [
    late.length ? h("section", { key: "overdue", class: "tasks-overdue", "aria-label": "Overdue tasks" },
      h("header", { class: "sec-head" }, h("h2", null, "Overdue"), h("span", { class: "sec-sub" }, String(late.length))),
      taskRows(late, true)) : null,
    h("section", { key: "upcoming", class: "tasks-upcoming", "aria-label": "Upcoming tasks" },
      h("header", { class: "sec-head" }, h("h2", null, "Upcoming Tasks"), upcoming.length ? h("span", { class: "sec-sub" }, String(upcoming.length)) : null,
        h("span", { class: "grow" }), button("Add", { kind: "ghost", small: true, icon: "plus", onClick: () => addTask(d) })),
      upcoming.length ? taskRows(upcoming, false) : h("p", { class: "quiet" }, "Nothing coming up.")),
  ];
}

function waitingSection(d) {
  return h("section", { "aria-label": "Waiting on you" },
      h("header", { class: "sec-head" }, h("h2", null, "Waiting on you"), d.waiting.length ? h("span", { class: "sec-sub" }, String(d.waiting.length)) : null),
      d.waiting.length ? d.waiting.map((e) => h("button", { type: "button", key: `${e.id}@${e.start}`, class: "row row-btn is-waiting",
        title: e.source === "luna" ? "Answer" : "Answer in Calendar", onClick: () => (e.source === "luna" ? go(`home/event/${e.id}`) : openEvent(e)) },
        h("span", { class: "row-time", "data-tip": longDay(e.start.slice(0, 10)) }, shortDay(e.start.slice(0, 10))),
        h("div", { class: "row-main" }, h("div", { class: "row-title" }, e.title),
          h("div", { class: "row-sub" }, [e.all_day ? "All day" : fmt.time(e.start), e.location].filter(Boolean).join(" · "))),
        h("span", { class: "row-end" }, e.rsvp === "maybe" ? "Maybe" : "No reply")))
        : h("p", { class: "quiet" }, d.calendar === "ready" ? "No invitations waiting on an answer." : "Connect Calendar to see invitations."));
}

// Sheet ------------------------------------------------------------------------------------------------------
// You and today, about now whatever the calendar shows: the date, the greeting, the weather, and what's next. Tasks and
// invitations follow under a hairline. The calendar already shows the day, so the sheet doesn't list it again.

function nextLine(d) {
  const g = d.glance, hour = new Date().getHours();
  if (g.next) {
    const until = toMin(g.next.start) - nowMin();
    return [h("span", null, "Next: "), h("span", { class: "hs-strong" }, `${g.next.title} at ${fmt.time(atMin(toMin(g.next.start)))}`),
      until > 0 && until < 180 ? h("span", null, `, in ${fmt.minutes(until)}`) : null];
  }
  if (hour >= 17 && g.tomorrow) return [h("span", null, "Tomorrow starts with "), h("span", { class: "hs-strong" }, `${g.tomorrow.title} at ${fmt.time(atMin(toMin(g.tomorrow.start)))}`)];
  return null;  // the calendar already says when the day is empty
}

function sheetHead(d, ctx) {
  const now = new Date(), hour = now.getHours();
  const part = hour >= 5 && hour < 12 ? "morning" : hour >= 12 && hour < 17 ? "afternoon" : "evening";
  const name = (settings.name || "").trim().split(/\s+/)[0];
  return h("section", { class: "hs-head", "aria-label": "Today" },
    h("p", { class: "hs-date" }, now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })),
    h("h1", null, `Good ${part}${name ? `, ${name}` : ""}`,
      // Until there's a name in Settings, the greeting offers to take you there.
      name ? null : h("button", { type: "button", class: "hs-name", title: "Set your name in Settings", onClick: () => go("settings") }, "Add your name")),
    weather(d.weather, ctx),
    (() => { const line = nextLine(d); return line ? h("p", { class: "hs-next" }, line) : null; })());
}

function weather(w, ctx) {
  if (w.state === "ask") {
    return h("div", { class: "hs-weather" }, button("Show weather", { kind: "ghost", small: true, icon: "location",
      title: "Uses this Mac's location, rounded to about 10 km",
      onClick: async () => { try { await api.post("/api/location/access", {}); ctx.reload(); } catch (e) { fail(e); } } }));
  }
  if (w.state !== "ready" || w.temp == null) return null;
  loadHours(ctx);
  const range_ = w.high != null && w.low != null ? `${w.high}° / ${w.low}°` : null;
  const rain = w.rain != null && w.rain >= 20 ? `${w.rain}% rain` : null;
  const hours = (wx.data && wx.data.hours) || [];
  return h("div", { class: ["hs-weather", wx.open && hours.length && "is-open"], tabindex: 0, "aria-label": "Weather; shows the hourly forecast",
    onMouseenter: () => openHours(ctx), onMouseleave: () => closeHours(ctx), onFocusin: () => openHours(ctx, 0), onFocusout: () => closeHours(ctx) },
  h("div", { class: "wx-now", "aria-hidden": wx.open ? "true" : null },
    icon(w.symbol, { class: "hs-wx-sym" }), h("b", null, `${w.temp}°`), h("span", null, [w.label, range_, rain].filter(Boolean).join(" · "))),
  hours.length ? hourly(hours) : null);
}

// Hourly forecast, in place: hover the weather for a moment and the reading fades into a strip of hours (two days back
// to a week ahead) that starts on the current hour. Drag it, or scroll, to move through the days; leaving fades back.
const HOUR_W = 44;
const wx = { open: false, data: null, at: 0, timer: null, fresh: false };

// Fetched with Home (every 10 minutes at most), so the strip is ready the moment you hover.
async function loadHours(ctx) {
  if (wx.at && Date.now() - wx.at < 10 * 60e3) return;
  wx.at = Date.now();
  try { wx.data = await api.get("/api/weather"); ctx.refresh(); } catch { wx.at = 0; }
}

function openHours(ctx, delay = 300) {
  clearTimeout(wx.timer);
  if (wx.open) return;
  wx.timer = setTimeout(() => { wx.open = true; wx.fresh = true; ctx.refresh(); }, delay);
}

function closeHours(ctx) {
  clearTimeout(wx.timer);
  wx.timer = setTimeout(() => { if (wx.open) { wx.open = false; ctx.refresh(); } }, 300);
}

// Lands on now each time it opens, pans on drag, and turns a vertical wheel into sideways movement.
function panner(nowIndex) {
  return (el) => {
    if (!el.__panner) {
      el.__panner = true;
      el.addEventListener("pointerdown", (e) => { el.__drag = { x: e.clientX, left: el.scrollLeft }; el.setPointerCapture(e.pointerId); el.classList.add("is-dragging"); });
      el.addEventListener("pointermove", (e) => { if (el.__drag) el.scrollLeft = el.__drag.left - (e.clientX - el.__drag.x); });
      const end = () => { el.__drag = null; el.classList.remove("is-dragging"); };
      el.addEventListener("pointerup", end);
      el.addEventListener("pointercancel", end);
      el.addEventListener("wheel", (e) => { if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { e.preventDefault(); el.scrollLeft += e.deltaY; } }, { passive: false });
    }
    if (wx.fresh) { wx.fresh = false; el.scrollTo({ left: nowIndex * HOUR_W, behavior: "instant" }); }
  };
}

function hourly(hours) {
  const now = new Date(), stamp = `${isoDay(now)}T${String(now.getHours()).padStart(2, "0")}:00`;
  const nowIndex = Math.max(0, hours.findIndex((x) => x.t === stamp));
  const dayName = (iso) => parseDay(iso).toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
  return h("div", { class: "wx-track", ref: panner(nowIndex), "aria-hidden": wx.open ? null : "true" },
    h("div", { class: "wx-strip", style: { width: `${hours.length * HOUR_W}px` } }, hours.map((x, i) => {
      const hr = Number(x.t.slice(11, 13));
      return h("div", { key: x.t, class: ["wx-hour", i < nowIndex && "is-past", i === nowIndex && "is-now", hr === 0 && "is-midnight"],
        title: `${x.label}${x.rain != null ? ` · ${x.rain}% chance of rain` : ""}` },
      // Midnight names its day, so you always know where you are.
      h("small", null, i === nowIndex ? "Now" : hr === 0 ? dayName(x.t.slice(0, 10)) : hourLabel(hr)),
      icon(x.symbol, { class: "wx-sym" }),
      h("b", null, `${x.temp}°`),
      h("span", { class: "wx-rain" }, x.rain != null && x.rain >= 30 ? `${x.rain}%` : ""));
    })));
}

function homeSheet(d, ctx) {
  return pane("home-sheet", { tag: "aside", class: "home-sheet", key: "home-sheet" }, sheetHead(d, ctx), h("div", { class: "hs-rule" }),
    taskSection(d), doneSection(d, ctx), waitingSection(d));
}

// Projects -----------------------------------------------------------------------------------------------------
// Pick up where you left off: pinned first, then what you touched last. Each kind shows what you'd reach for next; a
// project without a folder says, in its own terms, what linking one would give you.

const LINK_HINT = { music: "Play your latest mix", software: "See your last commit", writing: "Follow your word count",
  photography: "See your latest photos" };
const stem = (name) => name.replace(/\.[^.]+$/, "");

async function linkFolder(p) {
  const picked = await api.post("/api/pick", { kind: "folder", prompt: `Choose the folder for ${p.title}` }).catch(fail);
  if (picked && picked.path) await api.patch(`/api/projects/${p.id}`, { folder: picked.path }).catch(fail);
}

// What sits under the title: kind-specific, so a song reads as a song and a repo as a repo.
function projectMeta(p) {
  const n = p.next || {}, label = p.category_label || (CATEGORIES[p.category] || CATEGORIES.other)[0];
  if (n.kind === "audio") return [stem(n.file.name), `exported ${fmt.since(n.file.modified)}`];
  if (n.kind === "commit") return [n.branch, n.changes ? `${n.changes} uncommitted` : "clean", n.week ? `${fmt.plural(n.week, "commit")} this week` : fmt.since(n.at)];
  if (n.kind === "words") return [`${fmt.num(n.words)} words`, n.name, fmt.since(p.touched)];
  if (n.kind === "images") return [fmt.plural(n.count, "image"), `last export ${fmt.since(n.files[0].modified)}`];
  if (n.kind === "file") return [label, fmt.since(n.at)];
  return [label];
}

// A gentle line through the daily word counts; the shape matters, not the axis.
function trend(values) {
  if (!values || values.length < 2) return null;
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const points = values.map((v, i) => `${(i / (values.length - 1)) * 160},${30 - ((v - lo) / span) * 26}`).join(" ");
  return s("svg", { class: "pp-trend", viewBox: "0 0 160 34", preserveAspectRatio: "none", "aria-hidden": "true" },
    s("polyline", { points, fill: "none" }));
}

function cardBody(p, ctx) {
  const n = p.next || { kind: "empty" };
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  if (n.kind === "audio") {
    return h("div", { class: "pp-next pp-audio" }, playButton(n.file, p.title, true), waveform(n.file, { project: p.title, height: 34, refresh: ctx.refresh }));
  }
  if (n.kind === "commit") {
    return h("div", { class: "pp-commits" }, n.commits.map((c) => h("div", { key: c.hash, class: "pp-next" },
      h("span", { class: "pp-hash" }, c.hash), h("span", { class: "ellipsis pp-strong" }, c.subject), h("span", { class: "pp-age" }, fmt.ago(c.at)))));
  }
  if (n.kind === "words") {
    return h("div", { class: "pp-words" },
      h("div", null, h("b", null, n.week > 0 ? `+${fmt.num(n.week)}` : fmt.num(n.words)), h("span", null, n.week > 0 ? "words this week" : "words")),
      trend(n.history));
  }
  if (n.kind === "images") {
    return h("div", { class: "pp-thumbs" }, n.files.map((f) => h("img", { key: f.path, src: fileUrl(f.path), alt: "", loading: "lazy" })));
  }
  if (n.kind === "file") return h("div", { class: "pp-next" }, icon(n.symbol), h("span", { class: "ellipsis pp-strong" }, n.name));
  if (n.kind === "link") {
    return h("div", { class: "pp-next" }, h("span", { class: "ellipsis pp-quiet" }, LINK_HINT[p.category] || "See what changed"),
      button("Link folder", { kind: "ghost", small: true, class: "pp-action", onClick: stop(() => linkFolder(p)) }));
  }
  if (n.kind === "missing") {
    return h("div", { class: "pp-next" }, h("span", { class: "ellipsis tone-text-attention" }, "Folder not found"),
      button("Choose again", { kind: "ghost", small: true, class: "pp-action", onClick: stop(() => linkFolder(p)) }));
  }
  return h("div", { class: "pp-next pp-quiet" }, n.kind === "scanning" ? "Reading the folder…" : "Nothing in the folder yet");
}

function projectCards(d, ctx) {
  if (!d.projects.length) return null;
  return h("section", { class: "home-projects", "aria-label": "Pick up where you left off" },
    h("header", { class: "sec-head" }, h("h2", null, "Pick up where you left off")),
    h("div", { class: "pp-cards" }, d.projects.map((p) => {
      const open = () => go(`projects/${p.id}`);
      return h("div", { key: p.id, class: ["pane", "pp", "pp-card", `is-${p.category}`], "data-pane": `project-${p.id}`, "data-glass": "panel",
        style: hue(p.hue), role: "button", tabindex: 0, onClick: open, onKeydown: (e) => { if (e.key === "Enter" && e.target === e.currentTarget) open(); } },
      h("div", { class: "pp-head" }, tile(categorySymbol(p), p.hue),
        h("div", { class: "row-main" }, h("div", { class: "row-title pp-title" }, p.title),
          h("div", { class: "row-sub" }, projectMeta(p).filter(Boolean).join(" · "))),
        p.pinned && h("span", { class: "pp-pin", title: "Pinned" }, icon("pin.fill"))),
      cardBody(p, ctx));
    })));
}

export default {
  title: "Home",
  events: ["tasks", "projects", "courses", "calendar", "events", "people", "settings", "weather", "scan"],
  cached: (p) => (p[0] === "event" || !api.cached(url()) ? null : { ...api.cached(url()), panel: null }),
  load,
  render(d, ctx) {
    rerender = ctx.refresh;
    shown = d;
    if (!d || !d.period) return empty("calendar", "Loading your day", null);
    return h("div", { class: "home" }, d.panel ? eventPanel(d.panel, ctx) : homeSheet(d, ctx), h("div", { class: "desk" }, projectCards(d, ctx), calendar(d, ctx)));
  },
  create() { if (shown && shown.period) addTask(shown); },
};
