// Projects: a rail of projects (pinned, active, archived) beside the open one. A project is a name, a category, a
// description, a folder and its tasks; progress is done over all tasks, leaving out repeating ones. Its tasks and notes
// open in place, in the shared document view. Folders are read, never changed, except for the Notes folder Luna keeps.

import { h, s, api, fmt, go } from "../core.js";
import { icon, iconButton, button, empty, field, input, textarea, select, formData, openLayer, sheet, confirmDialog, menu, fail, toast } from "../ui.js";
import { play, seek, isCurrent, now, peaks, fileUrl, onPlayer } from "../player.js";
import { CATEGORIES, CATEGORY_HUES, tile, ownerChip, projectOwner, categoryLabel, categorySymbol, hue, taskRow, quickAdd, taskDoc, noteDoc,
  newNote, railItem, railIndicator, preloadEditor, lookPickers, swatchRow } from "../work.js";

const ui = { archived: false, done: false };
const waves = new Map();
let rerender = null;
onPlayer(() => rerender && location.hash.startsWith("#/projects") && rerender());

const urls = (params, list) => {
  let id = params[0] && params[0] !== "new" ? params[0] : null;
  if (list && (!id || !list.some((p) => p.id === id))) id = (list.find((p) => !p.archived) || list[0] || {}).id || null;
  return {
    id, project: id && `/api/projects/${id}`,
    task: params[1] === "task" && params[2] ? `/api/tasks/${params[2]}` : null,
    note: params[1] === "note" && params[2] ? `/api/notes/${params[2]}` : null,
  };
};

async function load(params) {
  const list = await api.get("/api/projects");
  const u = urls(params, list);
  const [project, task, note] = await Promise.all([u.project ? api.get(u.project).catch(() => null) : null,
    u.task ? api.get(u.task).catch(() => null) : null, u.note ? api.get(u.note).catch(() => null) : null]);
  preloadEditor();
  return { list, project, task, note };
}

// Rail ------------------------------------------------------------------------------------------------------------------

function rail(d) {
  const active = d.list.filter((p) => !p.archived), archived = d.list.filter((p) => p.archived);
  const item = (p) => railItem({ key: p.id, href: `projects/${p.id}`, selected: d.project && p.id === d.project.id,
    glyph: tile(categorySymbol(p), p.hue, "sm"), label: p.title, end: p.tasks_total ? fmt.pct(p.progress) : null, menu: () => projectMenu(p) });
  const pinned = active.filter((p) => p.pinned), rest = active.filter((p) => !p.pinned);
  return h("nav", { class: "pane rail", "data-pane": "side", "data-glass": "panel", "aria-label": "Projects" },
    h("header", { class: "rail-head" }, h("h2", null, "Projects"), iconButton("plus", { title: "New project (⌘N)", onClick: () => edit(null) })),
    h("div", { class: "rail-scroll" }, railIndicator("projects"),
      pinned.length ? [h("div", { class: "rail-group", key: "g-pinned" }, "Pinned"), pinned.map(item)] : null,
      rest.length ? [h("div", { class: "rail-group", key: "g-active" }, pinned.length ? "Active" : "Projects"), rest.map(item)] : null,
      !active.length ? h("p", { class: "rail-empty" }, "No open projects.") : null,
      ui.archived ? [h("div", { class: "rail-group", key: "g-archived" }, "Archived"), archived.map(item)] : null),
    archived.length ? h("button", { type: "button", class: "rail-item rail-foot", onClick: () => { ui.archived = !ui.archived; rerender && rerender(); } },
      icon("archivebox"), h("span", { class: "rail-label" }, ui.archived ? "Hide archived" : "Archived"), h("span", { class: "rail-end" }, String(archived.length))) : null);
}

// Overview --------------------------------------------------------------------------------------------------------------

function overview(p, ctx) {
  const open = p.tasks.filter((t) => t.status !== "done"), done = p.tasks.filter((t) => t.status === "done");
  const owner = projectOwner(p);
  const openTask = (t) => go(`projects/${p.id}/task/${t.id}`);
  const groups = [["doing", "In progress"], ["todo", "Not started"]];
  return h("section", { class: "pane work-main", "data-pane": "main", "data-glass": "panel", key: `project-${p.id}` },
    h("div", { class: "work-scroll" },
      h("header", { class: "doc-bar" }, ownerChip({ ...owner, label: categoryLabel(p) }), h("span", { class: "grow" }),
        iconButton(p.pinned ? "pin.fill" : "pin", { title: p.pinned ? "Unpin" : "Pin", active: p.pinned,
          onClick: () => api.patch(`/api/projects/${p.id}`, { pinned: !p.pinned }).catch(fail) }),
        p.folder ? button("Open folder", { icon: "folder", onClick: () => api.post("/api/open", { path: p.folder }).catch(fail) }) : null,
        iconButton("ellipsis", { title: "More", onClick: (e) => menu(e.currentTarget, [
          { label: "Edit project", icon: "pencil", onClick: () => edit(p) },
          p.folder && { label: "Rescan folder", icon: "arrow.clockwise", onClick: () => api.post(`/api/projects/${p.id}/scan`).then(() => ctx.reload()).catch(fail) },
          { label: p.archived ? "Restore" : "Archive", icon: "archivebox", onClick: () => api.patch(`/api/projects/${p.id}`, { archived: !p.archived })
            .then(() => toast(p.archived ? "Restored" : "Archived. Its repeating tasks stop repeating")).catch(fail) },
          "-",
          { label: "Delete project", icon: "trash", danger: true, onClick: () => remove(p) },
        ]) })),
      h("h1", { class: "work-title" }, p.title),
      p.description ? h("p", { class: "work-desc" }, p.description)
        : h("button", { type: "button", class: "work-desc is-empty", onClick: () => edit(p) }, "Add a description"),
      p.tasks_total ? h("div", { class: "progress-row", style: hue(p.hue) },
        h("div", { class: "meter" }, h("span", { style: { width: `${p.progress * 100}%` } })),
        h("b", { class: "num" }, `${p.tasks_done} of ${p.tasks_total}`), h("span", { class: "meta" }, "done")) : null,
      h("div", { class: "work-rule" }),
      h("div", { class: "work-grid" },
        h("div", { class: "work-tasks" },
          h("header", { class: "sec-row" }, h("h2", null, "Tasks"), h("span", { class: "grow" }),
            h("div", { class: "seg-row is-compact", role: "tablist" },
              h("button", { type: "button", class: !ui.done && "is-on", onClick: () => { ui.done = false; ctx.refresh(); } }, "Open", h("span", { class: "num seg-count" }, String(open.length))),
              h("button", { type: "button", class: ui.done && "is-on", onClick: () => { ui.done = true; ctx.refresh(); } }, "Done", h("span", { class: "num seg-count" }, String(done.length))))),
          ui.done
            ? (done.length ? done.sort((a, b) => (b.done_at || 0) - (a.done_at || 0)).map((t) => taskRow(t, { onOpen: openTask })) : h("p", { class: "quiet" }, "Nothing finished yet."))
            : [groups.map(([status, label]) => {
              const rows = open.filter((t) => t.status === status);
              return rows.length ? h("div", { key: status, class: "task-group" }, h("div", { class: "group-label" }, label), rows.map((t) => taskRow(t, { onOpen: openTask }))) : null;
            }), quickAdd("Add a task", (title) => api.post("/api/tasks", { title, project_id: p.id }))]),
        h("aside", { class: "work-side" }, latest(p, ctx), notesCard(p, owner), folderCard(p)))));
}

// What you'd reach for next, in the project's own terms.
function latest(p, ctx) {
  const n = p.next || {};
  if (n.kind === "audio") {
    return h("section", { class: "card", style: hue(p.hue) }, h("header", { class: "card-head" }, h("h3", null, "Latest mix"), h("span", { class: "meta" }, fmt.since(n.file.modified))),
      h("p", { class: "card-line" }, n.file.name),
      h("div", { class: "card-audio" }, playButton(n.file, p.title), waveform(n.file, { project: p.title, height: 28, refresh: ctx.refresh })));
  }
  if (n.kind === "commit") {
    return h("section", { class: "card" }, h("header", { class: "card-head" }, h("h3", null, n.branch), h("span", { class: "meta" }, n.changes ? `${n.changes} uncommitted` : "clean")),
      n.commits.map((c) => h("div", { key: c.hash, class: "card-file" }, h("span", { class: "mono meta" }, c.hash), h("span", { class: "ellipsis grow" }, c.subject), h("span", { class: "meta" }, fmt.ago(c.at)))));
  }
  if (n.kind === "words") {
    return h("section", { class: "card" }, h("header", { class: "card-head" }, h("h3", null, "Manuscript"), n.name ? h("span", { class: "meta ellipsis" }, n.name) : null),
      h("div", { class: "card-figure" }, h("b", { class: "num" }, fmt.num(n.words)), h("span", { class: "meta" }, n.week > 0 ? `+${fmt.num(n.week)} this week` : "words")));
  }
  if (n.kind === "images") {
    return h("section", { class: "card" }, h("header", { class: "card-head" }, h("h3", null, "Latest images"), h("span", { class: "meta" }, fmt.plural(n.count, "image"))),
      h("div", { class: "card-thumbs" }, n.files.map((f) => h("img", { key: f.path, src: fileUrl(f.path), alt: "", loading: "lazy" }))));
  }
  return null;
}

function notesCard(p, owner) {
  return h("section", { class: "card" },
    h("header", { class: "card-head" }, h("h3", null, "Notes"), h("span", { class: "grow" }),
      iconButton("plus", { small: true, title: "New note", onClick: async () => { const n = await newNote(owner); if (n) go(`projects/${p.id}/note/${n.id}`); } })),
    p.notes.length ? p.notes.slice(0, 6).map((n) => h("a", { key: n.id, href: `#/projects/${p.id}/note/${n.id}`, class: "card-file is-link" },
      h("span", { class: "file-glyph" }, icon("doc.text")), h("span", { class: "ellipsis grow" }, n.title), h("span", { class: "meta" }, fmt.ago(n.updated))))
      : h("p", { class: "card-empty" }, "Mix notes, lyrics, plans: anything longer than a task."));
}

function folderCard(p) {
  if (!p.folder) {
    return h("section", { class: "card" }, h("header", { class: "card-head" }, h("h3", null, "Folder")),
      h("p", { class: "card-empty" }, "Link the project's folder to see its latest work here."),
      button("Choose folder…", { small: true, onClick: () => pickFolder(p) }));
  }
  if (p.scan && p.scan.ok === false) {
    return h("section", { class: "card" }, h("header", { class: "card-head" }, h("h3", null, "Folder")),
      h("p", { class: "card-empty is-problem" }, "The folder isn't where it was."), button("Choose again…", { small: true, onClick: () => pickFolder(p) }));
  }
  const name = p.folder.split("/").filter(Boolean).pop();
  return h("section", { class: "card" },
    h("header", { class: "card-head" }, h("h3", null, "In the folder"), h("span", { class: "meta ellipsis", title: p.folder }, name)),
    (p.recent || []).length ? p.recent.map((f) => h("button", { type: "button", key: f.path, class: "card-file is-link", onClick: () => api.post("/api/open", { path: f.path }).catch(fail) },
      h("span", { class: "file-glyph" }, icon(FILE_ICONS[f.kind] || "doc")), h("span", { class: "ellipsis grow" }, f.name), h("span", { class: "meta" }, fmt.ago(f.modified))))
      : h("p", { class: "card-empty" }, "Nothing in it yet."));
}
const FILE_ICONS = { audio: "waveform", image: "photo", video: "film", document: "doc.text", session: "slider.horizontal.3", code: "chevron.left.forwardslash.chevron.right" };

async function pickFolder(p) {
  const picked = await api.post("/api/pick", { kind: "folder", prompt: `Choose the folder for ${p.title}` }).catch(fail);
  if (picked && picked.path) await api.patch(`/api/projects/${p.id}`, { folder: picked.path }).catch(fail);
}

// Right-click a project: a quick colour, then edit, pin, archive or delete.
export function projectMenu(p) {
  const patch = (data, done) => api.patch(`/api/projects/${p.id}`, data).then(() => done && toast(done)).catch(fail);
  return [
    swatchRow(p.color || p.hue, (color) => patch({ color })),
    { label: "Edit…", icon: "pencil", onClick: () => edit(p) },
    { label: p.pinned ? "Unpin" : "Pin", icon: p.pinned ? "pin.slash" : "pin", onClick: () => patch({ pinned: !p.pinned }) },
    { label: p.archived ? "Restore" : "Archive", icon: "archivebox", onClick: () => patch({ archived: !p.archived }, p.archived ? "Restored" : "Archived. Its repeating tasks stop repeating") },
    p.folder && { label: "Show in Finder", icon: "folder", onClick: () => api.post("/api/open", { path: p.folder, reveal: true }).catch(fail) },
    "-",
    { label: "Delete…", icon: "trash", danger: true, onClick: () => remove(p) },
  ];
}

async function remove(p) {
  if (!(await confirmDialog("Delete this project?", `“${p.title}” and its tasks will be gone. Notes and files in its folder stay.`))) return;
  try { await api.del(`/api/projects/${p.id}`); api.forget("/api/projects"); go("projects"); } catch (e) { fail(e); }
}

// Waveforms (shared with Home) ---------------------------------------------------------------------------------------

export function waveform(file, opts = {}) {
  const data = waves.get(file.path);
  if (data === undefined) {
    waves.set(file.path, null);
    peaks(file.path, file.size, 64).then((r) => { waves.set(file.path, r || false); const redraw = opts.refresh || rerender; if (redraw) redraw(); });
  }
  const values = data && data.peaks ? data.peaks : Array.from({ length: 64 }, (_, i) => 0.2 + 0.1 * Math.sin(i * 0.7));
  const w = 100 / values.length;
  const played = isCurrent(file.path) && now.duration ? now.time / now.duration : 0;
  const shape = (cls) => s("svg", { viewBox: "0 0 100 100", preserveAspectRatio: "none", class: cls },
    values.map((v, i) => { const hh = Math.max(6, v * 94); return s("rect", { x: i * w + w * 0.2, width: w * 0.6, y: 50 - hh / 2, height: hh, rx: 1 }); }));
  return h("div", { class: ["wave", !data && "is-loading"], style: { height: `${opts.height || 36}px`, "--played": `${played * 100}%` },
    onClick: (e) => {
      e.stopPropagation();
      if (!isCurrent(file.path)) { play({ path: file.path, name: file.name, project: opts.project }); return; }
      const r = e.currentTarget.getBoundingClientRect();
      seek((e.clientX - r.left) / r.width);
    } }, shape("wave-base"), shape("wave-played"));
}

export function playButton(file, project, small) {
  const playing = isCurrent(file.path) && now.playing;
  return h("button", { type: "button", class: ["play", small && "play-sm", playing && "is-playing"], title: playing ? "Pause" : "Play",
    onClick: (e) => { e.stopPropagation(); play({ path: file.path, name: file.name, project }); } }, icon(playing ? "pause.fill" : "play.fill", { weight: "bold" }));
}

// New and edit ------------------------------------------------------------------------------------------------------

export function edit(project) {
  const p = project || { category: "music", category_label: "" };
  let folder = p.folder || "", category = p.category;
  const look = { color: p.color || "", icon: p.icon || "" };
  openLayer((close, update) => sheet(project ? "Edit project" : "New project", close, h("div", { class: "form-grid" },
    field("Name", input({ name: "title", value: p.title || "", autofocus: true, placeholder: "Glass Hours", required: true }), { class: "span-2" }),
    field("Category", select("category", Object.entries(CATEGORIES).map(([k, v]) => [k, v[0]]), category,
      { onChange: (e) => { category = e.target.value; update(); } })),
    field("Label", input({ name: "category_label", value: p.category_label || "", placeholder: category === "other" ? "Design, Home…" : CATEGORIES[category][0], maxlength: 40 }),
      { hint: "Optional: your own name for the category" }),
    field("Description", textarea({ name: "description", value: p.description || "", rows: 2, placeholder: "What it is, in a sentence or two" }), { class: "span-2" }),
    lookPickers(look, update, { auto: CATEGORY_HUES[category], autoIcon: CATEGORIES[category][1] }),
    field("Folder", h("div", { class: "input-row" }, input({ value: folder, placeholder: "~/Music/Glass Hours", onInput: (e) => (folder = e.target.value) }),
      button("Choose…", { onClick: async () => { const r = await api.post("/api/pick", { kind: "folder", prompt: "Choose the project's folder" }).catch(fail); if (r && r.path) { folder = r.path; update(); } } })),
    { class: "span-2", hint: "Luna reads it and keeps the project's notes in a Notes folder inside." })),
  [h("span", { class: "grow" }), button("Cancel", { onClick: () => close() }), button(project ? "Save" : "Create", { kind: "primary", type: "submit" })],
  { onSubmit: async (form) => {
    const data = { ...formData(form), folder, color: look.color, icon: look.icon };
    if (!data.title) return;
    try {
      const row = project ? await api.patch(`/api/projects/${p.id}`, data) : await api.post("/api/projects", data);
      close();
      if (!project) go(`projects/${row.id}`);
    } catch (e) { fail(e); }
  } }), { width: 560, onClose: () => { if (location.hash === "#/projects/new") go("projects"); } });
}

export default {
  title: "Projects",
  events: ["projects", "tasks", "notes", "scan"],
  cached: (params) => {
    const list = api.cached("/api/projects");
    if (!list) return null;
    const u = urls(params, list);
    const project = u.project ? api.cached(u.project) : null, task = u.task ? api.cached(u.task) : null, note = u.note ? api.cached(u.note) : null;
    if ((u.project && !project) || (u.task && !task) || (u.note && !note)) return null;
    return { list, project, task, note };
  },
  load,
  render(d, ctx) {
    rerender = ctx.refresh;
    const p = d.project;
    let main;
    if (!p) {
      main = h("section", { class: "pane work-main is-empty", "data-pane": "main", "data-glass": "panel" },
        empty("square.stack.3d.up", "No projects yet", "A project is a name, a category and a folder. Tasks come later.",
          button("New project", { kind: "primary", onClick: () => edit(null) })));
    } else {
      const chip = ownerChip(projectOwner(p), { onClick: () => go(`projects/${p.id}`), title: "Back to the project" });
      if (d.task) main = taskDoc(d.task, { chip, onDeleted: () => go(`projects/${p.id}`) });
      else if (d.note) {
        const focus = newNote.fresh === d.note.id;
        newNote.fresh = null;
        main = noteDoc(d.note, { chip, focus, onDeleted: () => go(`projects/${p.id}`) });
      } else main = overview(p, ctx);
    }
    return h("div", { class: "work" }, rail(d), main);
  },
  focus(ctx) { if (ctx.params[0] === "new") edit(null); },
  create() { edit(null); },
};
