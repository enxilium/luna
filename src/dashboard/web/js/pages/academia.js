// Academia: courses on the left with their notes, the open note (or task, or the course itself) in the middle, and the
// course's coming events and open tasks on the right. A course is its code (CSC311), a description, a folder, a colour
// and an icon; notes are Markdown files in its folder. Courses have no progress: they end when you mark them finished.

import { h, api, fmt, go, isoDay, daysBetween } from "../core.js";
import { icon, iconButton, button, empty, field, input, textarea, formData, openLayer, sheet, confirmDialog, menu, fail, toast } from "../ui.js";
import { HUES, COURSE_ICONS, courseDefaults, tile, ownerChip, courseOwner, hue, taskRow, quickAdd, taskDoc, noteDoc, newNote,
  railItem, railIndicator, preloadEditor, dueChip, statusButton, lookPickers, swatchRow } from "../work.js";

const ui = { finished: false, opened: null, all: false, syncing: false };
let rerender = null;

const urls = (params, list) => {
  let id = params[0] && params[0] !== "new" ? params[0] : null;
  if (list && (!id || !list.some((c) => c.id === id))) id = (list.find((c) => c.active) || list[0] || {}).id || null;
  return {
    id, course: id && `/api/courses/${id}`,
    task: params[1] === "task" && params[2] ? `/api/tasks/${params[2]}` : null,
    note: params[1] === "note" && params[2] ? `/api/notes/${params[2]}` : null,
  };
};

async function load(params) {
  const list = await api.get("/api/courses");
  const u = urls(params, list);
  const [course, task, note] = await Promise.all([u.course ? api.get(u.course).catch(() => null) : null,
    u.task ? api.get(u.task).catch(() => null) : null, u.note ? api.get(u.note).catch(() => null) : null]);
  preloadEditor();
  return { list, course, task, note };
}

function rail(d) {
  const active = d.list.filter((c) => c.active), finished = d.list.filter((c) => !c.active);
  const open = d.course;
  const item = (c) => {
    const selected = open && c.id === open.id;
    return [railItem({ key: c.id, href: `academia/${c.id}`, selected: selected && !d.note && !d.task, glyph: tile(c.icon, c.color, "sm"), label: c.code,
      menu: () => courseMenu(c, d.list) }),
      selected ? [
        open.notes.map((n) => railItem({ key: `n-${n.id}`, href: `academia/${c.id}/note/${n.id}`, sub: true, selected: d.note && d.note.id === n.id, label: n.title,
          menu: () => [{ label: "Show in Finder", icon: "folder", onClick: () => api.post("/api/open", { path: n.path, reveal: true }).catch(fail) }, "-",
            { label: "Delete note…", icon: "trash", danger: true, onClick: async () => {
              if (!(await confirmDialog("Delete this note?", "The Markdown file moves to the Trash."))) return;
              await api.del(`/api/notes/${n.id}`).catch(fail);
              if (d.note && d.note.id === n.id) go(`academia/${c.id}`);
            } }] })),
        h("button", { type: "button", key: `new-${c.id}`, class: "rail-item is-sub is-add", onClick: async () => { const n = await newNote(courseOwner(c)); if (n) go(`academia/${c.id}/note/${n.id}`); } },
          icon("plus"), h("span", { class: "rail-label" }, "New note")),
      ] : null];
  };
  return h("nav", { class: "pane rail", "data-pane": "side", "data-glass": "panel", "aria-label": "Courses" },
    h("header", { class: "rail-head" }, h("h2", null, "Courses"), iconButton("plus", { title: "New course (⌘N)", onClick: () => edit(null, d.list) })),
    h("div", { class: "rail-scroll" }, railIndicator("courses"), active.map(item), !active.length ? h("p", { class: "rail-empty" }, "No courses this term.") : null,
      ui.finished ? [h("div", { class: "rail-group", key: "g-finished" }, "Finished"), finished.map(item)] : null),
    finished.length ? h("button", { type: "button", class: "rail-item rail-foot", onClick: () => { ui.finished = !ui.finished; rerender && rerender(); } },
      icon("archivebox"), h("span", { class: "rail-label" }, ui.finished ? "Hide finished" : "Finished"), h("span", { class: "rail-end" }, String(finished.length))) : null);
}

// The course itself: what it is, its notes, and everything still open.
function overview(c, d, ctx) {
  const owner = courseOwner(c);
  return h("section", { class: "pane work-main", "data-pane": "main", "data-glass": "panel", key: `course-${c.id}` },
    h("div", { class: "work-scroll" },
      // The code is said once, as the title; the rail already shows which course is open.
      h("header", { class: "title-row" }, h("h1", { class: "work-title" }, c.code), h("span", { class: "grow" }),
        c.folder ? button("Open folder", { icon: "folder", onClick: () => api.post("/api/open", { path: c.folder }).catch(fail) }) : null,
        iconButton("ellipsis", { title: "More", onClick: (e) => menu(e.currentTarget, [
          { label: "Edit course", icon: "pencil", onClick: () => edit(c, d.list) },
          { label: c.active ? "Mark as finished" : "Mark as active", icon: "archivebox", onClick: () => api.patch(`/api/courses/${c.id}`, { active: !c.active })
            .then(() => toast(c.active ? "Finished. Its repeating tasks stop repeating" : "Back in this term")).catch(fail) },
          "-",
          { label: "Delete course", icon: "trash", danger: true, onClick: () => remove(c) },
        ]) })),
      c.description ? h("p", { class: "work-desc" }, c.description)
        : h("button", { type: "button", class: "work-desc is-empty", onClick: () => edit(c, d.list) }, "Add what the course is, who teaches it, when it meets"),
      announcements(c),
      h("div", { class: "work-rule" }),
      h("header", { class: "sec-row" }, h("h2", null, "Notes"), h("span", { class: "grow" }),
        button("New note", { icon: "plus", small: true, onClick: async () => { const n = await newNote(owner); if (n) go(`academia/${c.id}/note/${n.id}`); } })),
      c.notes.length ? h("div", { class: "note-grid" }, c.notes.map((n) => h("a", { key: n.id, href: `#/academia/${c.id}/note/${n.id}`, class: "note-card" },
        h("span", { class: "note-card-title" }, n.title), h("span", { class: "meta" }, `Edited ${fmt.since(n.updated)}`))))
        : h("p", { class: "quiet" }, "Lecture notes, summaries and review sheets live here, as Markdown files in the course folder.")));
}

// Announcements mirrored from Quercus, newest first. Opening one shows it in place and marks it read; unread ones carry
// a dot. The header says when Quercus was last read and can read it again now.
function announcements(c) {
  const list = c.announcements || [], sync = c.quercus;
  if (!list.length && !sync) return null;
  const shown = ui.all ? list : list.slice(0, 5);
  const status = ui.syncing ? "Reading Quercus…" : !sync ? "" : sync.ok ? `Quercus · ${fmt.since(sync.at)}`
    : sync.reason === "token_rejected" || sync.reason === "token_setup_required" ? "Quercus needs a new token" : "Quercus unreachable";
  const refresh = async () => {
    ui.syncing = true; rerender && rerender();
    try { const r = await api.post("/api/quercus/sync", {}); if (r && !r.ok) toast("Couldn't read Quercus"); }
    catch (e) { fail(e); } finally { ui.syncing = false; api.forget(`/api/courses/${c.id}`); rerender && rerender(); }
  };
  const open = (a) => {
    ui.opened = ui.opened === a.id ? null : a.id;
    if (!a.read) { a.read = 1; api.patch(`/api/announcements/${a.id}`, { read: true }).catch(fail); }
    rerender && rerender();
  };
  return [
    h("header", { class: "sec-row", key: "ann-head" }, h("h2", null, "Announcements"), h("span", { class: "grow" }),
      status ? h("span", { class: ["sync-status", sync && !sync.ok && !ui.syncing && "is-problem"] }, status) : null,
      sync ? iconButton("arrow.clockwise", { small: true, title: "Read Quercus now", disabled: ui.syncing, onClick: refresh }) : null),
    list.length ? h("ul", { class: "ann-list", key: "ann-list" }, shown.map((a) => {
      const expanded = ui.opened === a.id;
      return h("li", { key: a.id, class: ["ann", !a.read && "is-unread", expanded && "is-open"] },
        h("button", { type: "button", class: "ann-head", "aria-expanded": String(expanded), onClick: () => open(a) },
          h("span", { class: "ann-dot", "aria-label": a.read ? null : "Unread" }),
          h("span", { class: "ann-title ellipsis" }, a.title),
          h("span", { class: "meta ann-when" }, fmt.since(a.posted_at))),
        expanded ? h("div", { class: "ann-body" },
          a.author ? h("p", { class: "meta" }, a.author) : null,
          h("p", { class: "ann-text" }, a.body || "No text."),
          a.url ? button(a.body.endsWith("...") ? "Read the rest on Quercus" : "Open on Quercus", { small: true, icon: "arrow.up.right",
            onClick: () => api.post("/api/open", { url: a.url }).catch(fail) }) : null) : null);
    }),
    list.length > 5 ? h("li", { key: "more" }, h("button", { type: "button", class: "ann-more", onClick: () => { ui.all = !ui.all; rerender && rerender(); } },
      ui.all ? "Show fewer" : `Show all ${list.length}`)) : null)
      : h("p", { class: "quiet", key: "ann-empty" }, "No announcements this term."),
  ];
}

// Right column: what's coming and what's open. (The course itself is the page's title, so it isn't repeated here.)
function side(c) {
  const now = new Date(), stamp = `${isoDay(now)}T${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
  const upcoming = c.events.filter((e) => e.end > stamp);
  const next = upcoming[0];
  const open = c.tasks.filter((t) => t.status !== "done").sort((a, b) => (a.due || "9") < (b.due || "9") ? -1 : 1);
  return h("aside", { class: "work-aside" },
    h("section", { class: "pane aside-card", "data-pane": "aside-events", "data-glass": "panel" },
      h("header", { class: "card-head" }, h("h3", null, "Coming up"), h("span", { class: "grow" }),
        iconButton("plus", { small: true, title: "New event", onClick: () => go(`home/event/new?course=${c.id}`) })),
      upcoming.length ? upcoming.slice(0, 5).map((e) => eventRow(e, e === next, c)) : h("p", { class: "card-empty" }, "No events coming up.")),
    h("section", { class: "pane aside-card grow-card", "data-pane": "aside-tasks", "data-glass": "panel" },
      h("header", { class: "card-head" }, h("h3", null, "Tasks"), h("span", { class: "grow" })),
      open.slice(0, 8).map((t) => h("div", { key: t.id, class: "mini-task", role: "button", tabindex: 0, onClick: () => go(`academia/${c.id}/task/${t.id}`) },
        statusButton(t), h("span", { class: "ellipsis grow" }, t.title), dueChip(t))),
      quickAdd("Add a task", (title) => api.post("/api/tasks", { title, course_id: c.id }))));
}

// Important events always stand out; the next one is lit only for being next.
function eventRow(e, isNext, c) {
  const start = new Date(e.start), day = isoDay(start), n = daysBetween(isoDay(), day);
  const when = e.all_day ? "All day" : fmt.time(e.start);
  const until = n === 0 ? (start > new Date() ? `in ${fmt.minutes(Math.round((start - new Date()) / 60000))}` : "Now") : n === 1 ? "Tomorrow" : null;
  return h("a", { key: e.id, href: `#/home/event/${e.id}`, class: ["event-row", e.important && "is-important", isNext && "is-next"], style: hue(c.color) },
    h("span", { class: "event-day" }, h("small", null, start.toLocaleDateString(undefined, { weekday: "short" })), h("b", { class: "num" }, String(start.getDate()))),
    h("span", { class: "event-main" }, h("span", { class: "event-title" }, e.important ? icon("star.fill", { class: "event-star" }) : null, e.title),
      h("span", { class: "event-sub" }, [when, e.location].filter(Boolean).join(" · "))),
    isNext && until ? h("span", { class: "event-until" }, until) : null);
}

// Right-click a course: a quick colour, then edit, finish or delete.
function courseMenu(c, list) {
  const patch = (data, done) => api.patch(`/api/courses/${c.id}`, data).then(() => done && toast(done)).catch(fail);
  return [
    swatchRow(c.color, (color) => patch({ color })),
    { label: "Edit…", icon: "pencil", onClick: () => edit(c, list) },
    { label: c.active ? "Mark as finished" : "Mark as active", icon: "archivebox", onClick: () => patch({ active: !c.active }, c.active ? "Finished. Its repeating tasks stop repeating" : "Back in this term") },
    c.folder && { label: "Show in Finder", icon: "folder", onClick: () => api.post("/api/open", { path: c.folder, reveal: true }).catch(fail) },
    "-",
    { label: "Delete…", icon: "trash", danger: true, onClick: () => remove(c) },
  ];
}

async function remove(c) {
  if (!(await confirmDialog("Delete this course?", `${c.code} and its tasks will be gone. Its notes stay in the folder; its events stay on the calendar.`))) return;
  try { await api.del(`/api/courses/${c.id}`); api.forget("/api/courses"); go("academia"); } catch (e) { fail(e); }
}

// New and edit: the code is the name. Colour and icon start from instant local defaults (see courseDefaults) and follow
// the code as you type until you pick one yourself.
export function edit(course, list = []) {
  const used = list.filter((x) => x.active && (!course || x.id !== course.id)).map((x) => x.color);
  const c = course || { code: "", ...courseDefaults("", used) };
  let code = c.code, folder = c.folder || "";
  const look = { color: c.color, icon: c.icon, chosen: !!course };
  openLayer((close, update) => sheet(course ? "Edit course" : "New course", close, h("div", { class: "form-grid course-form" },
    h("div", { class: "span-2 course-preview" }, tile(look.icon, look.color, "lg"),
      field("Code", input({ name: "code", value: code, autofocus: true, placeholder: "CSC311", required: true, maxlength: 24,
        onInput: (e) => { code = e.target.value; if (!look.chosen) Object.assign(look, courseDefaults(code, used)); update(); } }), { class: "grow" })),
    field("Description", textarea({ name: "description", value: c.description || "", rows: 2, placeholder: "Intro to Machine Learning · Prof. Grosse · MW 11–12" }), { class: "span-2" }),
    lookPickers(look, update, { icons: COURSE_ICONS }),
    field("Folder", h("div", { class: "input-row" }, input({ value: folder, placeholder: "~/School/CSC311", onInput: (e) => (folder = e.target.value) }),
      button("Choose…", { onClick: async () => { const r = await api.post("/api/pick", { kind: "folder", prompt: "Choose the course's folder" }).catch(fail); if (r && r.path) { folder = r.path; update(); } } })),
    { class: "span-2", hint: "Notes are saved as Markdown in a Notes folder inside." })),
  [h("span", { class: "grow" }), button("Cancel", { onClick: () => close() }), button(course ? "Save" : "Create", { kind: "primary", type: "submit" })],
  { onSubmit: async (form) => {
    const data = { ...formData(form), color: look.color, icon: look.icon, folder };
    if (!data.code) return;
    try {
      const row = course ? await api.patch(`/api/courses/${c.id}`, data) : await api.post("/api/courses", data);
      close();
      if (!course) go(`academia/${row.id}`);
    } catch (e) { fail(e); }
  } }), { width: 560, onClose: () => { if (location.hash === "#/academia/new") go("academia"); } });
}

export default {
  title: "Academia",
  events: ["courses", "tasks", "notes", "calendar", "quercus"],
  cached: (params) => {
    const list = api.cached("/api/courses");
    if (!list) return null;
    const u = urls(params, list);
    const course = u.course ? api.cached(u.course) : null, task = u.task ? api.cached(u.task) : null, note = u.note ? api.cached(u.note) : null;
    if ((u.course && !course) || (u.task && !task) || (u.note && !note)) return null;
    return { list, course, task, note };
  },
  load,
  render(d, ctx) {
    rerender = ctx.refresh;
    const c = d.course;
    if (!c) {
      return h("div", { class: "work is-single" }, rail(d), h("section", { class: "pane work-main is-empty", "data-pane": "main", "data-glass": "panel" },
        empty("graduationcap", "No courses yet", "Add a course by its code. Notes, tasks and events gather under it.",
          button("New course", { kind: "primary", onClick: () => edit(null, d.list) }))));
    }
    const chip = ownerChip(courseOwner(c), { onClick: () => go(`academia/${c.id}`), title: "Back to the course" });
    let main;
    if (d.task) main = taskDoc(d.task, { chip, onDeleted: () => go(`academia/${c.id}`) });
    else if (d.note) {
      const focus = newNote.fresh === d.note.id;
      newNote.fresh = null;
      main = noteDoc(d.note, { chip, focus, onDeleted: () => go(`academia/${c.id}`) });
    } else main = overview(c, d, ctx);
    return h("div", { class: "work has-aside" }, rail(d), main, side(c));
  },
  focus(ctx) { if (ctx.params[0] === "new") edit(null, api.cached("/api/courses") || []); },
  create() { edit(null, api.cached("/api/courses") || []); },
};
