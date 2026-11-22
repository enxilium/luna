// Shared pieces of Luna's productivity pages (Projects, Academia, tasks, Home): identity hues, owner chips, task status
// rings, due chips, priority bars, task rows, and the document view that notes and tasks share. A document is a title,
// a header that depends on what it is (a task's status, due date, priority and repeat; a note's date), and one
// Markdown editor (vendor/editor.js, CodeMirror with live preview and KaTeX).

import { h, s, api, fmt, go, isoDay, debounce, parseDay, daysBetween, transition } from "./core.js";
import { icon, iconButton, button, menu, fail, toast, openLayer, sheet, field, input, textarea, select, formData, confirmDialog, indicator } from "./ui.js";

export const CATEGORIES = {
  music: ["Music", "music.note"], software: ["Software", "chevron.left.forwardslash.chevron.right"],
  writing: ["Writing", "text.book.closed"], photography: ["Photography", "camera"], "3d": ["3D modeling", "cube"],
  other: ["Other", "folder"],
};
export const CATEGORY_HUES = { music: "rose", software: "lagoon", writing: "sand", photography: "peach", "3d": "orchid", other: "sky" };
export const HUES = ["iris", "orchid", "rose", "peach", "sand", "sage", "lagoon", "sky"];
export const COURSE_ICONS = ["book", "graduationcap", "function", "sum", "chart.bar", "cpu", "terminal", "atom", "flask", "leaf",
  "brain.head.profile", "globe.americas", "building.columns", "text.book.closed", "music.note", "paintbrush", "scalemass",
  "chart.line.uptrend.xyaxis", "bubble.left.and.text.bubble.right", "heart"];
// Mirrors store.SUBJECT_ICONS: the icon a new course gets from the letters of its code.
const SUBJECT_ICONS = { CSC: "cpu", CS: "cpu", COMP: "cpu", ECE: "cpu", MAT: "function", MATH: "function", APM: "function",
  STA: "chart.bar", STAT: "chart.bar", PHY: "atom", PHYS: "atom", AST: "atom", CHM: "flask", CHEM: "flask", BIO: "leaf",
  EEB: "leaf", PSY: "brain.head.profile", ECO: "chart.line.uptrend.xyaxis", ECON: "chart.line.uptrend.xyaxis",
  HIS: "building.columns", HIST: "building.columns", POL: "building.columns", ENG: "text.book.closed", LIT: "text.book.closed",
  PHL: "bubble.left.and.text.bubble.right", PHIL: "bubble.left.and.text.bubble.right", MUS: "music.note", FAH: "paintbrush",
  ART: "paintbrush", GGR: "globe.americas", GEO: "globe.americas", LIN: "bubble.left.and.text.bubble.right" };

// Mirrors store.course_defaults: the hue fewest active courses use (ties settle on the code), the icon from its subject.
export function courseDefaults(code, used = []) {
  const counts = Object.fromEntries(HUES.map((hue) => [hue, used.filter((u) => u === hue).length]));
  const fewest = Math.min(...Object.values(counts));
  const free = HUES.filter((hue) => counts[hue] === fewest);
  const seed = [...(code || "").toUpperCase()].reduce((n, c) => n + c.charCodeAt(0), 0);
  const letters = ((code || "").trim().match(/^[A-Za-z]*/) || [""])[0].toUpperCase();
  return { color: free[seed % free.length], icon: SUBJECT_ICONS[letters] || SUBJECT_ICONS[letters.slice(0, 3)] || "book" };
}

export const hue = (name) => ({ "--h": `var(--hue-${name || "sky"})` });
export const categoryLabel = (p) => p.category_label || (CATEGORIES[p.category] || CATEGORIES.other)[0];
export const categorySymbol = (p) => p.symbol || p.icon || (CATEGORIES[p.category] || CATEGORIES.other)[1];
// Mirrors store.ICONS: everything a project or course can wear.
export const ICONS = [...new Set([...COURSE_ICONS, ...Object.values(CATEGORIES).map(([, sym]) => sym), "waveform", "film", "house",
  "paintbrush.pointed", "gamecontroller", "hammer", "sparkles", "globe", "cart", "airplane"])];

// Colour and icon, like Notion's: eight swatches, then a grid of glyphs tinted in the chosen colour.
export function lookPickers(look, update, opts = {}) {
  const set = (patch) => { Object.assign(look, patch, { chosen: true }); update(); };
  return [
    field("Colour", h("div", { class: "swatches", role: "radiogroup" },
      opts.auto ? h("button", { type: "button", key: "auto", role: "radio", "aria-checked": String(!look.color), "aria-label": "Category colour",
        class: ["swatch is-auto", !look.color && "is-on"], style: hue(opts.auto), title: "The category's colour", onClick: () => set({ color: "" }) }) : null,
      HUES.map((x) => h("button", { type: "button", key: x, role: "radio", "aria-checked": String(x === look.color), "aria-label": x,
        class: ["swatch", x === look.color && "is-on"], style: hue(x), onClick: () => set({ color: x }) }))), { class: "span-2" }),
    field("Icon", h("div", { class: "icon-grid", role: "radiogroup" }, (opts.icons || ICONS).map((x) => h("button", { type: "button", key: x, role: "radio",
      "aria-checked": String(x === (look.icon || opts.autoIcon)), "aria-label": x, class: ["icon-pick", x === (look.icon || opts.autoIcon) && "is-on"],
      style: hue(look.color || opts.auto), onClick: () => set({ icon: x === opts.autoIcon ? "" : x }) }, icon(x)))), { class: "span-2" }),
  ];
}

// A right-click menu at the pointer. The first entry can be a row of colour swatches for a quick change.
export function contextMenu(e, items) {
  e.preventDefault();
  e.stopPropagation();
  const x = e.clientX, y = e.clientY;
  menu({ getBoundingClientRect: () => ({ right: x + 260, bottom: y - 8 }) }, items);
}
export function swatchRow(current, onPick) {
  return { custom: (close) => h("div", { class: "menu-swatches" }, HUES.map((x) => h("button", { type: "button", key: x, "aria-label": x,
    class: ["swatch is-sm", x === current && "is-on"], style: hue(x), onClick: () => { close(); onPick(x); } }))) };
}

// A glyph in a tile tinted with its owner's hue.
export function tile(symbol, hueName, size = "md") {
  return h("span", { class: ["hue-tile", `is-${size}`], style: hue(hueName) }, icon(symbol));
}

// The chip naming what something belongs to: a project (its category glyph) or a course (its own icon).
export function ownerChip(owner, opts = {}) {
  const symbol = owner.icon || owner.symbol || "folder";
  const body = [h("span", { class: "owner-glyph" }, icon(symbol)), h("span", null, owner.label)];
  return opts.onClick
    ? h("button", { type: "button", class: "owner-chip", style: hue(owner.hue), onClick: opts.onClick, title: opts.title }, body)
    : h("span", { class: "owner-chip", style: hue(owner.hue) }, body);
}

export const projectOwner = (p) => ({ kind: "project", id: p.id, label: p.title, hue: p.hue || p.color || CATEGORY_HUES[p.category], symbol: categorySymbol(p) });
export const courseOwner = (c) => ({ kind: "course", id: c.id, label: c.code, hue: c.color, icon: c.icon });

// Tasks ----------------------------------------------------------------------------------------------------------------

export const STATUSES = [["todo", "Not started"], ["doing", "In progress"], ["done", "Done"]];
export const PRIORITIES = [[0, "None"], [1, "Low"], [2, "Medium"], [3, "High"]];
export const REPEATS = [["daily", "Daily"], ["weekly", "Weekly"], ["biweekly", "Every 2 weeks"], ["monthly", "Monthly"]];
export const repeatLabel = (r) => (REPEATS.find(([v]) => v === r) || [null, null])[1];
const today = () => isoDay();
export const isLate = (t) => t.status !== "done" && t.due && t.due < today();
export const isSoon = (t) => t.status !== "done" && t.due && t.due >= today() && daysBetween(today(), t.due) <= 1;

// The status ring: not started is an open ring (coral when late, corona when due by tomorrow), in progress fills half
// in violet, done is a mint check.
export function statusGlyph(status, tone) {
  if (status === "done") {
    return s("svg", { viewBox: "0 0 18 18", class: "st st-done", "aria-hidden": "true" }, s("circle", { cx: 9, cy: 9, r: 8 }),
      s("path", { d: "M5.5 9.2 7.8 11.4 12.5 6.6" }));
  }
  return s("svg", { viewBox: "0 0 18 18", class: ["st", `st-${status}`, tone && `is-${tone}`], "aria-hidden": "true" },
    s("circle", { cx: 9, cy: 9, r: 7.25 }), status === "doing" ? s("path", { d: "M9 4a5 5 0 0 1 0 10z" }) : null);
}

export async function setStatus(task, status) {
  try {
    const row = await api.post(`/api/tasks/${task.id}/status`, { status });
    if (status === "done" && row.next) toast(`Done. Next one is due ${fmt.relativeLower(row.next.due)}`);
    else if (row.removed) toast("Back on your list. Removed the next one it had added");
    return row;
  } catch (e) { fail(e); return null; }
}

export function statusButton(task) {
  const tone = isLate(task) ? "late" : isSoon(task) ? "soon" : null;
  const label = STATUSES.find(([v]) => v === task.status)[1];
  return h("button", { type: "button", class: "status-btn", "aria-label": `${label}: ${task.title}`, title: task.status === "done" ? "Mark as not done" : "Mark as done",
    onClick: async (e) => {
      e.stopPropagation();
      const done = task.status !== "done", row = done && e.currentTarget.closest(".task-row, .mini-task, .row");
      if (row && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
        e.currentTarget.classList.add("is-completing");
        row.classList.add("is-completing");
        await new Promise((r) => setTimeout(r, 680));
      }
      setStatus(task, done ? "done" : "todo");
    },
    onContextmenu: (e) => { e.preventDefault(); e.stopPropagation(); statusMenu(e.currentTarget, task); } },
  statusGlyph(task.status, tone));
}

export function statusMenu(anchor, task) {
  menu(anchor, STATUSES.map(([value, label]) => ({ label, icon: value === task.status ? "checkmark" : null, onClick: () => setStatus(task, value) })));
}

export function dueLabel(due) {
  const n = daysBetween(today(), due);
  if (n === 0) return "Today";
  if (n === 1) return "Tomorrow";
  if (n === -1) return "Yesterday";
  const d = parseDay(due);
  if (n > 1 && n < 7) return `${d.toLocaleDateString(undefined, { weekday: "short" })} ${d.getDate()}`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: d.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined });
}

export function dueChip(task) {
  if (!task.due || task.status === "done") return null;
  const tone = isLate(task) ? "late" : isSoon(task) ? "soon" : "later";
  return h("span", { class: ["due-chip", `is-${tone}`], title: fmt.relativeDay(task.due) }, h("span", null, dueLabel(task.due)));
}

export function priorityBars(p) {
  if (!p) return null;
  return h("span", { class: "bars", "aria-label": `${PRIORITIES[p][1]} priority`, title: `${PRIORITIES[p][1]} priority` },
    [5, 8, 11].map((height, i) => h("i", { key: String(i), class: i < p && "is-on", style: { height: `${height}px` } })));
}

export function taskRow(task, opts = {}) {
  return h("div", { key: task.id, class: ["task-row", task.status === "done" && "is-done", opts.selected && "is-selected"],
    role: "button", tabindex: 0, onClick: () => opts.onOpen && opts.onOpen(task),
    onKeydown: (e) => { if (e.key === "Enter" && e.target === e.currentTarget) opts.onOpen && opts.onOpen(task); } },
  statusButton(task),
  h("span", { class: "task-title" }, task.title),
  opts.owner && task.owner ? h("span", { class: "task-owner", style: hue(task.owner.hue) }, task.owner.label) : null,
  task.description ? h("span", { class: "task-has-notes", title: "Has notes" }, icon("doc.text")) : null,
  task.repeat ? h("span", { class: "task-repeat", title: `Repeats ${repeatLabel(task.repeat).toLowerCase()}` }, icon("repeat"), repeatLabel(task.repeat)) : null,
  dueChip(task),
  h("span", { class: "task-priority" }, priorityBars(task.priority)));
}

// One line to add a task: type, Enter, and it's there (due later, from the task itself).
export function quickAdd(placeholder, create) {
  return h("label", { class: "quick-add-row" }, icon("plus"),
    h("input", { class: "quick-add-input", placeholder, autocomplete: "off", spellcheck: "false",
      onKeydown: async (e) => {
        if (e.key !== "Enter" || !e.target.value.trim()) return;
        const title = e.target.value.trim();
        e.target.value = "";
        try { await create(title); } catch (err) { fail(err); }
      } }));
}

// Documents (notes and tasks) ---------------------------------------------------------------------------------------

let editorModule = null;
function loadEditor() {
  if (!editorModule) {
    const css = document.createElement("link");
    css.rel = "stylesheet"; css.href = "/static/vendor/katex/katex.min.css";
    // Maths measured before KaTeX's styles apply comes out the wrong height, so the editor waits for them.
    const styled = new Promise((resolve) => { css.onload = css.onerror = resolve; });
    document.head.appendChild(css);
    editorModule = Promise.all([import("/static/vendor/editor.js"), styled]).then(([module]) => module);
  }
  return editorModule;
}
// Warm it while the page settles, so the first document opens without a wait.
export const preloadEditor = () => (window.requestIdleCallback ? window.requestIdleCallback(() => loadEditor()) : setTimeout(loadEditor, 300));

export const mediaUrl = (owner, rel) => (/^[a-z]+:/i.test(rel) ? rel : `/api/media?owner=${encodeURIComponent(owner)}&path=${encodeURIComponent(rel)}`);

async function upload(owner, file) {
  const response = await fetch(`/api/media?owner=${encodeURIComponent(owner)}&name=${encodeURIComponent(file.name || "pasted.png")}`,
    { method: "POST", body: file, credentials: "same-origin" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "That file couldn't be added.");
  return data;
}

// Documents open in Read mode, or Edit for a new or empty one, and change mode only when you ask (⌘E or the button in
// the bar): never because you clicked or the cursor moved. Edit mode is also the writing layout: the side panels slide
// away for room to write, and pointing at either window edge brings that panel back over the page.
const body = document.body.classList;
const EDGE = 16;
addEventListener("pointermove", (e) => {
  if (!body.contains("is-writing")) return;
  const panel = (side) => document.querySelector(side === "left" ? ".work > .rail" : ".work > .work-aside");
  for (const side of ["left", "right"]) {
    const el = panel(side), cls = `peek-${side}`;
    if (!el) continue;
    const atEdge = side === "left" ? e.clientX <= EDGE : e.clientX >= innerWidth - EDGE;
    if (atEdge && !body.contains(cls)) body.add(cls);
    else if (body.contains(cls) && !atEdge) {
      const r = el.getBoundingClientRect();
      if (e.clientX < r.left - 32 || e.clientX > r.right + 32 || e.clientY < r.top - 32 || e.clientY > r.bottom + 32) body.remove(cls);
    }
  }
});
const modes = new Map();  // document key → "read" | "edit", for this visit to the page
const writing = (on) => (on ? body.add("is-writing") : body.remove("is-writing", "peek-left", "peek-right"));
addEventListener("hashchange", () => { modes.clear(); writing(false); });

// The switch is one view transition: the page widens (or narrows) and the text changes form as one morph, with the line
// at the top of the page kept in place.
function setMode(host, next) {
  const editor = host.__editor, page = host.closest(".doc");
  if (!editor || !page || editor.mode === next) return;
  if (next === "read") host.__save?.flush();
  modes.set(page.dataset.key, next);
  const anchor = editor.anchor();
  transition(() => {
    page.dataset.mode = next;
    writing(next === "edit");
    editor.setMode(next, anchor);
  }, "writing");
}
const toggleMode = (host) => host?.__editor && setMode(host, host.__editor.mode === "edit" ? "read" : "edit");
addEventListener("keydown", (e) => {
  if (!e.metaKey || e.shiftKey || e.altKey || e.ctrlKey || e.key.toLowerCase() !== "e") return;
  const host = document.querySelector(".doc .doc-editor");
  if (!host?.__editor || document.querySelector("#overlay > *")) return;
  e.preventDefault();
  e.stopPropagation();
  toggleMode(host);
}, true);

// Unsaved typing is flushed before leaving the page.
const pending = new Set();
addEventListener("hashchange", () => { for (const save of pending) save.flush(); });
addEventListener("pagehide", () => { for (const save of pending) save.flush(); });

function editorHost(doc) {
  return h("div", { class: "doc-editor", key: `editor-${doc.key}`, "data-keep": doc.key, ref: (el) => {
    if (el.__loading) return;
    el.__loading = true;
    let timer = null, latest = null;
    const save = { flush() { if (timer == null) return; clearTimeout(timer); timer = null; pending.delete(save); doc.onBody(latest); } };
    el.__save = save;
    loadEditor().then(({ createEditor }) => {
      const mode = modes.get(doc.key) || doc.mode;
      el.__editor = createEditor(el, {
        doc: doc.body || "", label: doc.label, placeholder: doc.placeholder, mode,
        resolve: (rel) => mediaUrl(doc.media, rel), upload: (file) => upload(doc.media, file),
        onChange: (text) => { latest = text; pending.add(save); clearTimeout(timer); timer = setTimeout(() => save.flush(), 500); },
        onBlur: () => save.flush(),
      });
      if (mode === "edit") {
        writing(true);
        el.__editor.focus(true);
      }
    }).catch(fail);
  } });
}

// The page every note and task opens on. `header` is what makes it a task or a note; everything else is shared.
export function docView(doc) {
  const saveTitle = debounce((value) => doc.onTitle(value), 400);
  doc.mode = doc.focus || !(doc.body || "").trim() ? "edit" : "read";
  const mode = modes.get(doc.key) || doc.mode;
  return h("article", { class: "pane doc", "data-pane": "main", "data-glass": "panel", key: `doc-${doc.key}`,
    "data-key": doc.key, "data-mode": mode },
    h("header", { class: "doc-bar" }, doc.crumbs, h("span", { class: "grow" }),
      doc.meta ? h("span", { class: "meta" }, doc.meta) : null,
      h("button", { type: "button", class: "icon-btn doc-mode", "aria-label": "Switch between reading and editing (⌘E)",
        title: "Read or edit  ⌘E", onClick: (e) => toggleMode(e.currentTarget.closest(".doc").querySelector(".doc-editor")) },
        icon("pencil", { class: "doc-mode-edit" }), icon("book", { class: "doc-mode-read" })),
      doc.menu ? iconButton("ellipsis", { title: "More", onClick: (e) => menu(e.currentTarget, doc.menu) }) : null),
    h("div", { class: "doc-scroll" }, h("div", { class: "doc-page" },
      h("textarea", { class: "doc-title", rows: 1, value: doc.title, placeholder: "Untitled", "aria-label": "Title", spellcheck: "true",
        ref: (el) => { el.style.height = "auto"; el.style.height = `${el.scrollHeight}px`; },
        onInput: (e) => { e.target.style.height = "auto"; e.target.style.height = `${e.target.scrollHeight}px`; saveTitle(e.target.value); },
        onKeydown: (e) => {
          if (e.key !== "Enter") return;
          e.preventDefault();
          saveTitle.flush(e.target.value);
          const editor = e.target.closest(".doc").querySelector(".doc-editor").__editor;
          if (editor?.mode === "edit") editor.focus();
        },
        onBlur: (e) => saveTitle.flush(e.target.value) }),
      doc.header,
      h("div", { class: "doc-rule" }),
      editorHost(doc))));
}

// A task's header: status, due date, priority and repeat, each saved as you change it.
export function taskHeader(task) {
  const patch = (data) => api.patch(`/api/tasks/${task.id}`, data).catch(fail);
  const segmentedRow = (options, value, onPick, render) => h("div", { class: "seg-row", role: "radiogroup" }, options.map(([v, label]) =>
    h("button", { type: "button", key: String(v), role: "radio", "aria-checked": String(v === value), class: v === value && "is-on", onClick: () => v !== value && onPick(v) },
      render ? render(v) : null, label)));
  return h("div", { class: "doc-props" },
    h("div", { class: "prop" }, h("span", { class: "prop-label" }, "Status"),
      segmentedRow(STATUSES, task.status, (v) => setStatus(task, v), (v) => statusGlyph(v))),
    h("div", { class: "prop" }, h("span", { class: "prop-label" }, "Due"),
      h("div", { class: "prop-due" },
        h("input", { type: "date", class: "prop-date", value: task.due || "", "aria-label": "Due date", onChange: (e) => patch({ due: e.target.value || null }) }),
        task.due ? h("span", { class: ["prop-hint", isLate(task) ? "is-late" : isSoon(task) ? "is-soon" : null] }, fmt.relativeDay(task.due)) : null)),
    h("div", { class: "prop" }, h("span", { class: "prop-label" }, "Priority"),
      segmentedRow(PRIORITIES, task.priority || 0, (v) => patch({ priority: v }), (v) => (v ? priorityBars(v) : null))),
    h("div", { class: "prop" }, h("span", { class: "prop-label" }, "Repeats"),
      h("div", { class: "prop-repeat" },
        h("button", { type: "button", role: "switch", class: ["switch", task.repeat && "is-on"], "aria-checked": String(!!task.repeat), "aria-label": "Repeats",
          onClick: () => patch({ repeat: task.repeat ? "" : "weekly" }) }, h("i")),
        task.repeat ? select("repeat", REPEATS, task.repeat, { class: "prop-select", onChange: (e) => patch({ repeat: e.target.value }) })
          : h("span", { class: "prop-hint" }, "Once"))));
}

export function taskDoc(task, opts) {
  return docView({
    key: `task-${task.id}`, title: task.title, body: task.description, media: task.media, label: "Task notes",
    placeholder: "Add notes, images or files. Type / for blocks",
    crumbs: [opts.chip, h("span", { class: "crumb-sep" }, icon("chevron.right")), h("span", { class: "crumb" }, "Task")],
    meta: `Edited ${fmt.since(task.updated)}`,
    header: taskHeader(task),
    onTitle: (title) => title.trim() && api.patch(`/api/tasks/${task.id}`, { title: title.trim() }).catch(fail),
    onBody: (description) => api.patch(`/api/tasks/${task.id}`, { description }).catch(fail),
    menu: [{ label: "Delete task", icon: "trash", danger: true, onClick: async () => {
      if (!(await confirmDialog("Delete this task?", `“${task.title}” and its notes will be gone.`))) return;
      await api.del(`/api/tasks/${task.id}`).catch(fail);
      opts.onDeleted && opts.onDeleted();
    } }],
  });
}

export function noteDoc(note, opts) {
  return docView({
    key: `note-${note.id}`, title: note.title, body: note.body, media: note.media, label: "Note", focus: opts.focus,
    crumbs: [opts.chip, h("span", { class: "crumb-sep" }, icon("chevron.right")), h("span", { class: "crumb" }, "Note")],
    meta: `Edited ${fmt.since(note.updated)}`,
    header: h("p", { class: "doc-sub" }, `Created ${fmt.date(note.created, { weekday: "long", month: "long", day: "numeric" })}`),
    onTitle: (title) => api.patch(`/api/notes/${note.id}`, { title: title.trim() || "Untitled" }).catch(fail),
    onBody: (body) => api.patch(`/api/notes/${note.id}`, { body }).catch(fail),
    menu: [
      { label: "Show in Finder", icon: "folder", onClick: () => api.post("/api/open", { path: note.path, reveal: true }).catch(fail) },
      "-",
      { label: "Delete note", icon: "trash", danger: true, onClick: async () => {
        if (!(await confirmDialog("Delete this note?", "The Markdown file moves to the Trash."))) return;
        await api.del(`/api/notes/${note.id}`).catch(fail);
        opts.onDeleted && opts.onDeleted();
      } },
    ],
  });
}

export async function newNote(owner) {
  try {
    const note = await api.post("/api/notes", { title: "Untitled", [`${owner.kind}_id`]: owner.id });
    newNote.fresh = note.id;  // the next render of this note puts the cursor in it
    return note;
  } catch (e) { fail(e); return null; }
}

// Rows for a rail list: glyph in its hue, name, and an optional figure on the right.
export function railItem(opts) {
  return h("a", { href: `#/${opts.href}`, key: opts.key, class: ["rail-item", opts.selected && "is-selected", opts.sub && "is-sub"],
    onContextmenu: opts.menu ? (e) => contextMenu(e, opts.menu()) : null },
    opts.glyph, h("span", { class: "rail-label" }, opts.label), opts.end != null ? h("span", { class: "rail-end" }, opts.end) : null);
}

export { field, input, textarea, select, formData, openLayer, sheet, button, go };

// The selected row's highlight, gliding between rows of a rail. Put it first inside the rail's scroll area.
export const railIndicator = (key) => indicator(key, ".rail-item.is-selected", "rail-indicator");
