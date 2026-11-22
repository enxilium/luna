// Shell: the floating top bar, routing, live refresh, search and notifications.

import { h, s, mount, api, listen, connectEvents, route, go, settings, springEasing, debounce, reducedMotion, transition } from "./core.js";
import { icon, iconButton, openLayer, closeTopLayer, hasLayer, installTooltips, fail, menu, indicator } from "./ui.js";
import { install as installGlass, sync, configure, setShortcut } from "./glass.js";
import { refresh as refreshBackdrop } from "./backdrop.js";
import home from "./pages/home.js";
import projects from "./pages/projects.js";
import academia from "./pages/academia.js";
import task from "./pages/task.js";
import preferences from "./pages/settings.js";
import setup from "./pages/setup.js";
import { openInbox } from "./pages/chat.js";

const PAGES = { home, projects, academia, settings: preferences, task, setup };
const NAV = [["home", "Home", "house"], ["projects", "Projects", "square.stack.3d.up"], ["academia", "Academia", "graduationcap"],
  ["settings", "Settings", "gearshape"]];

const state = { route: route(), data: null, error: null, unread: 0, alerts: [], version: 0, nav: 0, navigating: false };
const ctx = {
  get params() { return state.route.params; },
  refresh: () => render(),
  reload: () => load(true),
  // For changes of layout the user asked for (a calendar view, a list filter): animate between the two states.
  animate: (motion = "swap") => transition(render, motion),
  reloadAnimated: (motion = "swap") => load(true, motion),
  refreshUnread: () => refreshBadges(),
  go,
};
const page = () => PAGES[state.route.name] || home;

// A refresh of the page on screen. While a navigation is loading the next page, refreshes wait for it: the navigation
// renders fresh data anyway, and an old page's reload must never cancel it.
async function load(silent, animate) {
  if (state.navigating) return;
  const version = ++state.version;
  try {
    const data = await page().load(state.route.params, ctx);
    if (version !== state.version || state.navigating) return;
    state.data = data; state.error = null;
  } catch (error) {
    if (version !== state.version) return;
    state.error = error;
    if (silent) fail(error);
  }
  if (animate) transition(render, animate); else render();
}

// Luna's mark from the notch: a thin ring with one bright bead, here at rest.
function mark() {
  return s("svg", { class: "mark", viewBox: "0 0 16 16", role: "img", "aria-label": "Luna" },
    s("circle", { class: "mark-ring", cx: 8, cy: 8, r: 5 }), s("circle", { class: "mark-bead", cx: 8 + 5 * Math.SQRT1_2, cy: 8 - 5 * Math.SQRT1_2, r: 1.75 }));
}

function topbar() {
  const active = state.pending || state.route.name;  // the tab answers the click at once, before the page has loaded
  const initials = (settings.name || "Me").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  return h("header", { class: "topbar" },
    h("div", { class: "topbar-left" }, mark()),
    h("nav", { class: "nav" }, indicator("nav", ".nav-item.is-active", "nav-indicator"), NAV.map(([id, label]) => h("a", {
      href: `#/${id}`, key: id, class: ["nav-item", id === active && "is-active"], "data-pane": `nav-${id}`, "data-glass": "pill",
    }, h("span", { class: "nav-label" }, label)))),
    h("div", { class: "topbar-right" },
      iconButton("bubble.left.and.bubble.right", { pane: "top-messages", title: "Messages", badge: state.unread, onClick: () => openInbox(ctx) }),
      iconButton("bell", { pane: "top-alerts", title: "Notifications", badge: state.alerts.length, problem: true, onClick: (e) => showAlerts(e.currentTarget) }),
      iconButton("magnifyingglass", { pane: "top-search", title: "Search (⌘K)", onClick: () => openPalette() }),
      h("button", { type: "button", class: "me", title: settings.name || "Settings", onClick: () => go("settings") }, initials)));
}

function main() {
  const p = page();
  let content;
  if (state.error && !state.data) content = h("div", { class: "page-head" }, h("div", null, h("h1", null, p.title), h("p", null, state.error.message)));
  else if (state.data == null) content = h("div");
  else content = p.render(state.data, ctx);
  return h("main", { class: "page", id: "page", key: `page-${state.route.name}` },
    h("div", { class: "page-body", key: "body" }, content));
}

function render() {
  const setting = state.route.name === "setup";
  mount(document.getElementById("app"), h("div", { class: ["app", setting && "is-setup"] }, topbar(), main()));
  sync();
}

// What a navigation means decides how it moves (glass.css → Motion): another top-level page slides along the tab order;
// within a page, a deeper level (a task or note opened from a project, an event from Home) steps forward and back steps
// out; another item at the same level (the next project, another note) fades through. What stays the same holds still.
function motionFor(from, to) {
  if (from.name !== to.name) {
    const a = NAV.findIndex(([id]) => id === from.name), b = NAV.findIndex(([id]) => id === to.name);
    if (a < 0 || b < 0) return b < 0 ? "deeper" : "shallower";
    return b > a ? "forward" : "back";
  }
  // A page's own first level ("#/projects" shows the first project) is the same level as picking one ("#/projects/<id>").
  const depth = (r) => Math.max(1, r.params.length);
  if (depth(to) > depth(from)) return "deeper";
  if (depth(to) < depth(from)) return "shallower";
  return "swap";
}

async function navigate() {
  const next = route();
  if (!PAGES[next.name]) { go("home"); return; }
  const samePage = next.name === state.route.name;
  const changed = !samePage || next.params.join("/") !== state.route.params.join("/");
  if (!changed && state.data) { state.route = next; return; }
  const target = PAGES[next.name], nav = ++state.nav;
  state.navigating = true;
  if (next.name !== state.route.name && NAV.some(([id]) => id === next.name)) { state.pending = next.name; render(); }
  const cached = target.cached ? target.cached(next.params) : null;
  let data = cached, error = null;
  if (data == null) { try { data = await target.load(next.params, ctx); } catch (e) { error = e; } }
  if (nav !== state.nav) return;  // a newer navigation took over
  state.navigating = false;
  state.version++;  // anything still loading for the old page is stale
  const motion = motionFor(state.route, next);
  if (next.name === "setup" && data) setup.start(data);
  await transition(() => { state.route = next; state.pending = null; state.data = data; state.error = error; render(); }, motion);
  if (cached != null) load(true);
  page().focus && requestAnimationFrame(() => page().focus(ctx));
}

async function refreshBadges() {
  try {
    const [chats, alerts] = await Promise.all([api.get("/api/messages/chats").catch(() => ({ chats: [] })), api.get("/api/alerts")]);
    state.unread = chats.chats.filter((c) => !c.muted).reduce((n, c) => n + (c.unread || 0), 0);
    state.alerts = alerts;
    render();
  } catch { /* badges are best-effort */ }
}

const ALERT_ICONS = { task: "checklist", birthday: "gift", milestone: "flag", draft: "doc.badge.ellipsis" };
function showAlerts(anchor) {
  if (!state.alerts.length) {
    menu(anchor, [{ label: "You're all caught up", icon: "checkmark.circle", onClick: () => {} }]);
    return;
  }
  menu(anchor, state.alerts.map((a) => ({ label: a.title, meta: a.meta, icon: ALERT_ICONS[a.kind] || "bell", onClick: () => go(a.link) })));
}

// Command palette ----------------------------------------------------------------------------------------------

const ACTIONS = [
  { label: "New project", icon: "plus.square.on.square", run: () => go("projects/new") },
  { label: "New course", icon: "graduationcap", run: () => go("academia/new") },
  { label: "New event", icon: "calendar.badge.plus", run: () => go("home/event/new") },
  { label: "Open messages", icon: "bubble.left.and.bubble.right", run: () => openInbox(ctx) },
];
const KIND = { project: ["square.stack.3d.up", "Projects"], course: ["graduationcap", "Courses"], task: ["checklist", "Tasks"],
  note: ["doc.text", "Notes"], event: ["calendar", "Events"], person: ["person", "People"],
  chat: ["bubble.left", "Conversations"] };

function openPalette() {
  if (hasLayer()) return;
  let query = "", results = [], active = 0;
  const items = () => {
    const q = query.trim().toLowerCase();
    const nav = NAV.map(([id, label, symbol]) => ({ group: "Go to", label, icon: symbol, run: () => go(id) })).filter((i) => !q || i.label.toLowerCase().includes(q));
    const actions = ACTIONS.filter((a) => !q || a.label.toLowerCase().includes(q)).map((a) => ({ ...a, group: "Actions" }));
    const hits = results.filter((r) => KIND[r.kind]).map((r) => ({ group: KIND[r.kind][1], label: r.title, icon: KIND[r.kind][0], run: () => openHit(r) }));
    return [...hits, ...actions, ...nav];
  };
  const search = debounce(async () => {
    results = query.trim().length < 2 ? [] : await api.get(`/api/search?q=${encodeURIComponent(query)}`).catch(() => []);
    active = 0; layer.update();
  }, 120);
  const run = (item) => { layer.close(); item.run(); };
  const layer = openLayer(() => {
    const list = items();
    active = Math.min(active, Math.max(0, list.length - 1));
    let group = null;
    return h("div", { class: "palette" },
      h("div", { class: "palette-input" }, icon("magnifyingglass"), h("input", { autofocus: true, value: query, placeholder: "Search Luna",
        onInput: (e) => { query = e.target.value; active = 0; layer.update(); search(); },
        onKeydown: (e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); active = Math.max(0, Math.min(list.length - 1, active + (e.key === "ArrowDown" ? 1 : -1))); layer.update(); }
          else if (e.key === "Enter" && list[active]) { e.preventDefault(); run(list[active]); }
        } }), h("span", { class: "kbd" }, "esc")),
      h("div", { class: "palette-list" }, list.map((item, i) => {
        const head = item.group !== group ? h("div", { class: "palette-group", key: `g-${item.group}` }, item.group) : null;
        group = item.group;
        return [head, h("button", { type: "button", key: `${item.group}-${i}`, class: ["palette-item", i === active && "is-active"],
          onClick: () => run(item), onPointermove: () => { if (active !== i) { active = i; layer.update(); } } }, icon(item.icon), item.label,
        i === active && h("span", { class: "kbd is-live palette-enter" }, "⏎"))];
      })),
      h("div", { class: "palette-foot" }, h("span", null, "↑ ↓  Move"), h("span", null, "⏎  Open")));
  }, { width: 620, class: "layer-palette" });
}

function openHit(hit) {
  const target = hit.link || { project: `projects/${hit.id}`, course: `academia/${hit.id}` }[hit.kind];
  if (target) go(target);
}

// Boot ---------------------------------------------------------------------------------------------------------

async function boot() {
  installGlass();
  const spring = springEasing();
  if (CSS.supports("transition-timing-function", spring.easing)) {
    document.documentElement.style.setProperty("--spring", spring.easing);
    document.documentElement.style.setProperty("--spring-ms", `${spring.duration}ms`);
  }
  installTooltips();
  try { Object.assign(settings, await api.get("/api/settings")); } catch { /* defaults */ }
  applyGlass();
  addEventListener("hashchange", navigate);
  document.addEventListener("keydown", (e) => {
    const typing = e.target.closest("input, textarea, select, .cm-editor");
    if (e.key === "Escape" && e.defaultPrevented) return;  // the editor closed its own menu
    if (e.key === "Escape") { e.preventDefault(); if (!closeTopLayer()) { if (typing) e.target.blur(); } return; }
    if (e.metaKey && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
    if (e.metaKey && /^[1-9]$/.test(e.key) && NAV[Number(e.key) - 1]) { e.preventDefault(); go(NAV[Number(e.key) - 1][0]); return; }
    if (e.metaKey && e.key === ",") { e.preventDefault(); go("settings"); return; }
    if (e.metaKey && e.key.toLowerCase() === "n" && page().create) { e.preventDefault(); page().create(ctx); }
  });
  const refresh = debounce(() => load(true), 160);
  listen((event) => {
    if (event.type === "settings") api.get("/api/settings").then((v) => { Object.assign(settings, v); applyGlass(); render(); });
    if (["messages", "tasks", "people", "calendar"].includes(event.type)) refreshBadges();
    if (page().events && page().events.includes(event.type)) refresh();
  });
  connectEvents();
  // The overlay was hidden, maybe for hours: catch up when it comes back.
  addEventListener("luna-show", async () => {
    // Until Luna can listen, showing the dashboard means finishing setup.
    if (state.route.name !== "setup" && !(await api.get("/api/setup").catch(() => ({ voice_ready: true }))).voice_ready) { go("setup"); return; }
    load(true); refreshBadges();
  });
  addEventListener("luna-shortcut", (e) => { if (!e.detail) fail(new Error("That shortcut is already taken by another app. Pick another in Settings.")); });
  if (!location.hash) history.replaceState(null, "", "#/home");
  // First run (or anything voice needs gone missing): open on setup.
  const ready = await api.get("/api/setup").then((d) => d.voice_ready, () => true);
  if (!ready && route().name !== "setup") history.replaceState(null, "", "#/setup");
  state.route = route();
  if (!PAGES[state.route.name]) { history.replaceState(null, "", "#/home"); state.route = route(); }
  try { state.data = await page().load(state.route.params, ctx); } catch (e) { state.error = e; }
  if (state.route.name === "setup" && state.data) setup.start(state.data);
  render();
  page().focus && page().focus(ctx);
  refreshBadges();
  // Warm every other tab's data once things are quiet, so switching tabs plays its transition at once instead of
  // waiting on the server (a page shows its cached data, then refreshes it quietly).
  const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 800));
  idle(() => NAV.forEach(([id]) => { if (id !== state.route.name && PAGES[id].load) PAGES[id].load([], ctx).catch(() => {}); }));
}

// Mirrors src/dashboard/hotkey.py SHORTCUTS.
export const SHORTCUTS = { "ctrl+option+space": "⌃⌥Space", "option+space": "⌥Space", "cmd+shift+space": "⇧⌘Space",
  "ctrl+option+l": "⌃⌥L", "cmd+shift+l": "⇧⌘L" };

export function applyGlass() {
  const tint = Number(settings.glass_tint ?? 0.32);
  const style = settings.glass || "frosted";
  document.documentElement.style.setProperty("--tint", String(style === "clear" ? tint * 0.5 : style === "dark" ? tint + 0.3 : tint));
  document.body.dataset.glass = style;
  document.documentElement.style.setProperty("--backdrop-blur", style === "clear" ? "28px" : "56px");
  refreshBackdrop();
  configure(style, tint);
  setShortcut(settings.overlay_shortcut || "ctrl+option+space");
}

boot();
