// Components. Anything glass is a [data-pane] with a `data-glass` kind (panel, pill, chip, sheet, menu); glass.css
// styles them over the window's frosted background. Text is white with a soft shadow so it holds up over any wallpaper.

import { h, s, mount, classes, fmt, reducedMotion } from "./core.js";
import { sync } from "./glass.js";

export function icon(name, opts = {}) {
  return h("span", { class: ["sym", opts.class], "aria-hidden": "true",
    style: { "--sym": `url("/sym/${name}?w=${opts.weight || "regular"}")`, fontSize: opts.size ? `${opts.size}px` : null } });
}

// A glass surface. `id` names its role (stat-1, main, side…); layouts reuse ids so pages keep a familiar shape.
export function pane(id, opts, ...children) {
  return h(opts.tag || "section", {
    class: ["pane", opts.class], "data-pane": id, "data-glass": opts.glass || "panel", key: opts.key || id,
    style: opts.style, onClick: opts.onClick, "data-z": opts.z,
  }, children);
}

export function paneHead(title, opts = {}) {
  return h("header", { class: "pane-head" },
    h("div", { class: "pane-titles" }, h("h2", null, title), opts.sub && h("div", { class: "pane-sub" }, opts.sub)),
    opts.action);
}

export function button(label, opts = {}) {
  const glass = opts.kind === "glass";
  return h("button", {
    type: opts.type || "button", class: ["btn", `btn-${opts.kind || "plain"}`, opts.small && "btn-sm", opts.class],
    onClick: opts.onClick, title: opts.title, disabled: opts.disabled, key: opts.key,
    "data-pane": glass ? opts.pane : null, "data-glass": glass ? "pill" : null,
  }, opts.icon && icon(opts.icon), label && h("span", null, label));
}

export function iconButton(name, opts = {}) {
  return h("button", {
    type: "button", class: ["icon-btn", opts.small && "icon-btn-sm", opts.active && "is-active", opts.class], onClick: opts.onClick,
    title: opts.title, "aria-label": opts.title, disabled: opts.disabled, key: opts.key,
    "data-pane": opts.pane, "data-glass": opts.pane ? "chip" : null,
  }, icon(name, { weight: opts.weight || "regular" }), opts.badge ? h("span", { class: ["badge", opts.problem && "is-problem"] }) : null);
}

export function segmented(id, options, value, onChange) {
  const index = Math.max(0, options.findIndex((o) => o.value === value));
  return h("div", { class: "segmented", "data-pane": id, "data-glass": "pill", key: id,
    style: { "--count": options.length, "--index": index } },
  h("span", { class: "segmented-thumb" }),
  options.map((o) => h("button", { type: "button", key: String(o.value), class: { "is-active": o.value === value },
    onClick: () => o.value !== value && onChange(o.value) }, o.label)));
}

export function tabs(options, value, onChange) {
  return h("div", { class: "tabs", role: "tablist" }, options.map((o) => h("button", {
    type: "button", key: String(o.value), class: ["tab", o.value === value && "is-active"], onClick: () => onChange(o.value) },
  o.label, o.count ? h("span", { class: "tab-count" }, o.count) : null)));
}

export function chip(label, opts = {}) {
  return h("button", { type: "button", class: ["chip", opts.active && "is-active"], onClick: opts.onClick, key: opts.key },
    opts.icon && icon(opts.icon), h("span", null, label), opts.count != null && h("span", { class: "chip-count" }, opts.count));
}

export function pill(label, tone) {
  return h("span", { class: ["pill", tone && `tone-${tone}`] }, label);
}

// Change against the previous period, as plain text: the notch keeps colour for state, not for direction.
export function delta(now, before) {
  if (!before) return now ? h("span", { class: "delta" }, "New") : null;
  const change = (now - before) / Math.abs(before);
  if (!isFinite(change)) return null;
  return h("span", { class: "delta" }, `${change >= 0 ? "+" : "−"}${Math.abs(change * 100).toFixed(change && Math.abs(change) < 0.1 ? 1 : 0)}%`);
}

export function check(done, onToggle, opts = {}) {
  return h("button", { type: "button", class: ["check", done && "is-done"], "aria-pressed": String(!!done),
    "aria-label": opts.label || (done ? "Mark as not done" : "Mark as done"),
    onClick: (e) => { e.stopPropagation(); onToggle(!done); } },
  s("svg", { viewBox: "0 0 20 20", width: 18, height: 18 }, s("circle", { cx: 10, cy: 10, r: 8.3, class: "check-ring" }),
    s("path", { d: "M6.2 10.4 8.8 13 14 7.6", class: "check-tick" })));
}

export function progress(value) {
  return h("div", { class: "progress" }, h("span", { style: { width: `${Math.max(0, Math.min(1, value || 0)) * 100}%` } }));
}

export function avatar(name, opts = {}) {
  const clean = (name || "?").replace(/[^\p{L}\p{N} ]/gu, "").trim();
  const initials = clean.split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "#";
  const size = opts.size || 30;
  return h("span", { class: ["avatar", opts.class], style: { width: `${size}px`, height: `${size}px`, fontSize: `${Math.round(size * 0.36)}px` } }, initials);
}

const NETWORKS = {
  imessage: ["message.fill", "iMessage"], sms: ["bubble.left.fill", "SMS"], discord: ["gamecontroller.fill", "Discord"],
  instagram: ["camera.fill", "Instagram"], whatsapp: ["phone.bubble.fill", "WhatsApp"], telegram: ["paperplane.fill", "Telegram"],
  signal: ["lock.fill", "Signal"], messenger: ["bolt.fill", "Messenger"], slack: ["number", "Slack"],
  linkedin: ["briefcase.fill", "LinkedIn"], beeper: ["bubble.left.and.bubble.right.fill", "Beeper"], other: ["bubble.fill", "Chat"],
};
export const networkName = (n) => (NETWORKS[n] || [null, n ? n[0].toUpperCase() + n.slice(1) : "Chat"])[1];
export function networkBadge(network) {
  return h("span", { class: ["net", `net-${network}`], title: networkName(network) }, icon((NETWORKS[network] || NETWORKS.other)[0], { weight: "semibold" }));
}

export function empty(symbol, title, text, action) {
  return h("div", { class: "empty" }, h("div", { class: "empty-icon" }, icon(symbol, { weight: "light" })),
    h("div", { class: "empty-title" }, title), text && h("p", null, text), action);
}

// Fields ----------------------------------------------------------------------------------------------------------

export function field(label, control, opts = {}) {
  return h("label", { class: ["field", opts.class] }, h("span", { class: "field-label" }, label), control,
    opts.hint && h("span", { class: "field-hint" }, opts.hint));
}
export const input = (props) => h("input", { class: "input", autocomplete: "off", spellcheck: "false", ...props });
export const textarea = (props) => h("textarea", { class: "input textarea", ...props });
export function select(name, options, value, props = {}) {
  return h("select", { name, ...props, class: ["input select", props.class] }, options.map((o) => {
    const [v, label] = Array.isArray(o) ? o : [o, o];
    return h("option", { value: v, selected: String(v) === String(value ?? "") }, label);
  }));
}
export function formData(form) {
  const data = {};
  for (const [key, value] of new FormData(form).entries()) data[key] = typeof value === "string" ? value.trim() : value;
  return data;
}

// Layers ----------------------------------------------------------------------------------------------------------

let layers = [], layerSeq = 0;

// A glass sheet over the page. `render(close, update)` returns its content.
export function openLayer(render, opts = {}) {
  const kind = opts.kind || "dialog";
  const id = `layer-${++layerSeq}`;
  const layer = { closed: false };
  const scrim = h("div", { class: ["scrim", `scrim-${kind}`], onClick: (e) => { if (e.target === scrim) close(); } });
  const panel = h("div", { class: ["layer", `layer-${kind}`, opts.class], "data-pane": id, "data-glass": kind === "popover" ? "menu" : "sheet",
    "data-z": 10 + layerSeq, role: "dialog", style: opts.width ? { width: `min(${opts.width}px, calc(100vw - 40px))` } : null });
  scrim.appendChild(panel);
  const update = () => { if (!layer.closed) { mount(panel, render(close, update)); sync(); } };
  function close(result) {
    if (layer.closed) return;
    layer.closed = true;
    layers = layers.filter((l) => l !== layer);
    scrim.classList.add("is-closing");
    scrim.classList.remove("is-in");
    sync({ animate: true });
    setTimeout(() => { scrim.remove(); sync(); }, reducedMotion() ? 0 : 220);
    opts.onClose && opts.onClose(result);
  }
  Object.assign(layer, { close, update, panel });
  layers.push(layer);
  document.getElementById("overlay").appendChild(scrim);
  update();
  if (opts.position) opts.position(panel);
  requestAnimationFrame(() => {
    scrim.classList.add("is-in");
    sync({ animate: true, follow: 300 });
    const focus = panel.querySelector("[autofocus]");
    if (focus) focus.focus({ preventScroll: true });
  });
  return layer;
}

export function closeTopLayer() { const top = layers[layers.length - 1]; if (top) { top.close(); return true; } return false; }
export const hasLayer = () => layers.length > 0;

export function sheet(title, close, body, actions, opts = {}) {
  return h("form", { class: "sheet", onSubmit: (e) => { e.preventDefault(); opts.onSubmit && opts.onSubmit(e.currentTarget); } },
    h("header", { class: "sheet-head" }, h("div", null, h("h2", null, title), opts.sub && h("div", { class: "pane-sub" }, opts.sub)),
      opts.headAction, iconButton("xmark", { title: "Close", onClick: () => close() })),
    h("div", { class: "sheet-body" }, body),
    actions && h("footer", { class: "sheet-foot" }, actions));
}

export function confirmDialog(title, text, label = "Delete") {
  return new Promise((resolve) => {
    openLayer((close) => sheet(title, close, h("p", { class: "muted" }, text),
      [h("span", { class: "grow" }), button("Cancel", { onClick: () => close(false) }), button(label, { kind: "danger", type: "submit" })],
      { onSubmit: () => close(true) }), { width: 400, onClose: (r) => resolve(!!r) });
  });
}

export function menu(anchor, items) {
  const r = anchor.getBoundingClientRect();
  openLayer((close) => h("div", { class: "menu" }, items.filter(Boolean).map((item) => item === "-" ? h("div", { class: "menu-sep" }) :
    item.custom ? item.custom(close) :
    h("button", { type: "button", class: ["menu-item", item.danger && "is-danger"], onClick: () => { close(); item.onClick(); } },
      item.icon && icon(item.icon), h("span", { class: "grow" }, item.label), item.meta && h("span", { class: "faint" }, item.meta)))),
  { kind: "popover", width: 260, position: (panel) => {
    panel.style.left = `${Math.min(innerWidth - 272, Math.max(12, r.right - 260))}px`;
    panel.style.top = `${Math.max(12, Math.min(r.bottom + 8, innerHeight - panel.offsetHeight - 12))}px`;
  } });
}

export function toast(message, tone) {
  const el = h("div", { class: ["toast", tone && `tone-${tone}`], role: "status" },
    icon(tone === "bad" ? "exclamationmark.circle.fill" : "checkmark.circle.fill"), h("span", null, message));
  document.getElementById("toasts").appendChild(el);
  requestAnimationFrame(() => el.classList.add("is-in"));
  setTimeout(() => { el.classList.remove("is-in"); setTimeout(() => el.remove(), 300); }, tone === "bad" ? 5000 : 2600);
}
export const fail = (error) => toast(error.message || String(error), "bad");

export function installTooltips() {
  const tip = h("div", { class: "tooltip", role: "tooltip" });
  document.body.appendChild(tip);
  let timer, current;
  document.addEventListener("pointerover", (e) => {
    const target = e.target.closest("[data-tip]");
    if (target === current) return;
    current = target;
    clearTimeout(timer);
    tip.classList.remove("is-in");
    if (!target) return;
    timer = setTimeout(() => {
      tip.textContent = target.getAttribute("data-tip");
      const r = target.getBoundingClientRect();
      tip.style.left = `${Math.min(innerWidth - tip.offsetWidth - 8, Math.max(8, r.left + r.width / 2 - tip.offsetWidth / 2))}px`;
      tip.style.top = `${r.top - tip.offsetHeight - 8 < 8 ? r.bottom + 8 : r.top - tip.offsetHeight - 8}px`;
      tip.classList.add("is-in");
    }, 220);
  });
  document.addEventListener("pointerdown", () => { clearTimeout(timer); tip.classList.remove("is-in"); current = null; });
}

export { classes, fmt };

// A highlight that glides to whichever item is active. It's its own element, behind the items: labels never move or
// fade, only the highlight travels. It renders where it last was (so a re-render doesn't reset it), then moves.
const placed = new Map();
export function indicator(key, selector, cls) {
  const at = placed.get(key);
  return h("span", { class: ["indicator", cls], key: `indicator-${key}`, "aria-hidden": "true",
    style: at ? { transform: `translate(${at.x}px, ${at.y}px)`, width: `${at.w}px`, height: `${at.h}px`, opacity: at.on ? 1 : 0 } : { opacity: 0 },
    ref: (el) => requestAnimationFrame(() => {
      const target = el.parentElement && el.parentElement.querySelector(selector);
      const next = target ? { x: target.offsetLeft, y: target.offsetTop, w: target.offsetWidth, h: target.offsetHeight, on: true }
        : { ...(placed.get(key) || { x: 0, y: 0, w: 0, h: 0 }), on: false };
      const first = !placed.get(key) || !placed.get(key).on;
      placed.set(key, next);
      if (first) el.style.transition = "none";  // appearing: no slide in from nowhere
      Object.assign(el.style, { transform: `translate(${next.x}px, ${next.y}px)`, width: `${next.w}px`, height: `${next.h}px`, opacity: next.on ? 1 : 0 });
      if (first) { el.getBoundingClientRect(); el.style.transition = ""; }
    }) });
}
