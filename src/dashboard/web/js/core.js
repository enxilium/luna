// Core runtime: element builder, keyed DOM morphing, API + live events, routing, formatting and motion.

const SVG = "http://www.w3.org/2000/svg";
const PROPS = new Set(["value", "checked", "selected", "disabled", "indeterminate"]);

function build(ns, tag, props, children) {
  const el = ns ? document.createElementNS(ns, tag) : document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value == null || value === false) continue;
      if (key === "class") el.setAttribute("class", classes(value));
      else if (key === "style") typeof value === "string" ? el.setAttribute("style", value) : el.setAttribute("style", styleText(value));
      else if (key === "key") el.setAttribute("data-key", value);
      else if (key === "ref") el.__ref = value;
      else if (key === "html") el.innerHTML = value;
      else if (key.startsWith("on") && typeof value === "function") on(el, key.slice(2).toLowerCase(), value);
      else if (PROPS.has(key) && !ns) { el[key] = value; if (key === "value") el.__v = String(value); else el.setAttribute(key, ""); }
      else el.setAttribute(key, value === true ? "" : value);
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false || child === true) continue;
    el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function classes(value) {
  if (Array.isArray(value)) return value.filter(Boolean).map(classes).join(" ");
  if (typeof value === "object") return Object.entries(value).filter(([, v]) => v).map(([k]) => k).join(" ");
  return String(value);
}

function styleText(style) {
  return Object.entries(style).filter(([, v]) => v != null && v !== false)
    .map(([k, v]) => `${k.startsWith("--") ? k : k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase())}:${v}`).join(";");
}

// Listeners live in el.__on so morphing can swap handlers without re-binding the DOM.
function dispatch(event) { const fn = this.__on && this.__on[event.type]; if (fn) fn.call(this, event); }
function on(el, type, fn) {
  el.__on = el.__on || {};
  if (!el.__on[type]) el.addEventListener(type, dispatch);
  el.__on[type] = fn;
}

export const h = (tag, props, ...children) => build(null, tag, props, children);
export const s = (tag, props, ...children) => build(SVG, tag, props, children);
export const frag = (...children) => { const f = document.createDocumentFragment(); append(f, children); return f; };

const keyOf = (n) => (n.nodeType === 1 ? n.getAttribute("data-key") : null);

export function morph(from, to) {
  if (from.nodeType !== to.nodeType || from.nodeName !== to.nodeName || keyOf(from) !== keyOf(to)) {
    from.replaceWith(to);
    runRefs(to);
    return to;
  }
  if (from.nodeType === 3 || from.nodeType === 8) {
    if (from.data !== to.data) from.data = to.data;
    return from;
  }
  if (from.hasAttribute("data-keep") && to.hasAttribute("data-keep") && from.getAttribute("data-keep") === to.getAttribute("data-keep")) return from;
  for (const { name } of [...from.attributes]) if (!to.hasAttribute(name)) from.removeAttribute(name);
  for (const { name, value } of to.attributes) if (from.getAttribute(name) !== value) from.setAttribute(name, value);
  if (to.__on) for (const [type, fn] of Object.entries(to.__on)) on(from, type, fn);
  if (from.__on) for (const type of Object.keys(from.__on)) if (!to.__on || !to.__on[type]) from.__on[type] = null;
  if (from.tagName === "INPUT" || from.tagName === "TEXTAREA" || from.tagName === "SELECT") {
    // Compare with the last *rendered* value, not the live one: typing never gets overwritten, but a model
    // change (clearing a sent draft) still lands even while the field has focus.
    const next = to.__v;
    if (next !== undefined && next !== from.__v && from.value !== next) from.value = next;
    from.__v = next;
    if (from.checked !== to.checked) from.checked = to.checked;
    if (from.tagName === "TEXTAREA") return from;
  }
  if (to.__ref) { from.__ref = to.__ref; }
  morphChildren(from, to);
  if (from.__ref) from.__ref(from);
  return from;
}

function morphChildren(from, to) {
  const keyed = new Map();
  for (const child of from.childNodes) { const k = keyOf(child); if (k) keyed.set(k, child); }
  const next = [...to.childNodes];
  let cursor = from.firstChild;
  for (const child of next) {
    const k = keyOf(child);
    let match = null;
    if (k) match = keyed.get(k) || null;
    else if (cursor && !keyOf(cursor) && cursor.nodeName === child.nodeName) match = cursor;
    if (match) {
      if (match !== cursor) from.insertBefore(match, cursor);
      else cursor = cursor.nextSibling;
      if (k) keyed.delete(k);
      const result = morph(match, child);
      if (result !== match && cursor === match) cursor = result.nextSibling;
    } else {
      from.insertBefore(child, cursor);
      runRefs(child);
    }
  }
  while (cursor) { const nextNode = cursor.nextSibling; cursor.remove(); cursor = nextNode; }
}

function runRefs(node) {
  if (node.nodeType !== 1) return;
  if (node.__ref) node.__ref(node);
  for (const el of node.querySelectorAll("*")) if (el.__ref) el.__ref(el);
}

export function mount(target, node) {
  if (!target.firstChild) { target.appendChild(node); runRefs(node); return node; }
  if (target.childNodes.length > 1) while (target.childNodes.length > 1) target.lastChild.remove();
  return morph(target.firstChild, node);
}

// API ------------------------------------------------------------------------------------------------------------

const cache = new Map();

async function request(method, path, body) {
  const response = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined, credentials: "same-origin",
  });
  let data = null;
  try { data = await response.json(); } catch { /* empty body */ }
  if (!response.ok) throw new Error((data && data.error) || "Something went wrong.");
  return data;
}

export const api = {
  get: async (path) => { const data = await request("GET", path); cache.set(path, data); return data; },
  cached: (path) => cache.get(path),
  post: (path, body = {}) => request("POST", path, body),
  put: (path, body = {}) => request("PUT", path, body),
  patch: (path, body = {}) => request("PATCH", path, body),
  del: (path) => request("DELETE", path),
  forget: (prefix) => { for (const key of cache.keys()) if (key.startsWith(prefix)) cache.delete(key); },
};

const listeners = new Set();
export function listen(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = (event) => {
    let data; try { data = JSON.parse(event.data); } catch { return; }
    for (const fn of listeners) fn(data);
  };
  return source;
}

// Routing --------------------------------------------------------------------------------------------------------

export function route() {
  const [name = "today", ...rest] = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  return { name, params: rest.map(decodeURIComponent) };
}
export const go = (path) => { if (location.hash !== "#/" + path) location.hash = "#/" + path; };

// Formatting -----------------------------------------------------------------------------------------------------

export const settings = { units: "metric", currency: "USD", name: "" };
const locale = navigator.language || "en-US";

export function parseDay(day) { const [y, m, d] = day.split("-").map(Number); return new Date(y, m - 1, d); }
export function isoDay(date = new Date()) {
  const p = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}`;
}
export function addDays(date, n) { const d = new Date(date); d.setDate(d.getDate() + n); return d; }
export function daysBetween(a, b) { return Math.round((parseDay(b) - parseDay(a)) / 86400000); }

export const fmt = {
  num: (v, digits = 0) => v == null ? "–" : Number(v).toLocaleString(locale, { maximumFractionDigits: digits, minimumFractionDigits: digits }),
  compact: (v) => v == null ? "–" : Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 }).format(v),
  money: (v, opts = {}) => v == null ? "–" : Intl.NumberFormat(locale, { style: "currency", currency: settings.currency || "USD",
    maximumFractionDigits: opts.cents === false || Math.abs(v) >= 1000 ? 0 : 2, minimumFractionDigits: 0 }).format(v),
  pct: (v) => v == null ? "–" : `${Math.round(v * 100)}%`,
  duration: (seconds) => {
    if (!seconds) return "0m";
    const h = Math.floor(seconds / 3600), m = Math.round((seconds % 3600) / 60);
    return h ? (m ? `${h}h ${m}m` : `${h}h`) : `${m}m`;
  },
  minutes: (m) => fmt.duration((m || 0) * 60),
  distance: (meters) => {
    if (!meters) return "–";
    return settings.units === "imperial" ? `${fmt.num(meters / 1609.344, 1)} mi` : `${fmt.num(meters / 1000, 1)} km`;
  },
  weight: (kg, digits = 1) => kg == null ? "–" : settings.units === "imperial" ? `${fmt.num(kg / 0.45359237, digits)} lb` : `${fmt.num(kg, digits)} kg`,
  weightValue: (kg) => settings.units === "imperial" ? kg / 0.45359237 : kg,
  weightUnit: () => settings.units === "imperial" ? "lb" : "kg",
  pace: (seconds, meters) => {
    if (!seconds || !meters) return "–";
    const per = seconds / (settings.units === "imperial" ? meters / 1609.344 : meters / 1000);
    return `${Math.floor(per / 60)}:${String(Math.round(per % 60)).padStart(2, "0")} /${settings.units === "imperial" ? "mi" : "km"}`;
  },
  bytes: (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${fmt.num(n / 1024)} KB` : n < 1073741824 ? `${fmt.num(n / 1048576, 1)} MB` : `${fmt.num(n / 1073741824, 1)} GB`,
  date: (value, opts = { month: "short", day: "numeric" }) => {
    if (!value) return "";
    const d = typeof value === "number" ? new Date(value * 1000) : value.length === 10 ? parseDay(value) : new Date(value);
    return d.toLocaleDateString(locale, opts);
  },
  time: (value) => new Date(typeof value === "number" ? value * 1000 : value).toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" }),
  weekday: (day) => parseDay(day).toLocaleDateString(locale, { weekday: "short" }),
  relativeDay: (day) => {
    if (!day) return "";
    const n = daysBetween(isoDay(), day);
    if (n === 0) return "Today";
    if (n === 1) return "Tomorrow";
    if (n === -1) return "Yesterday";
    if (n > 1 && n < 7) return parseDay(day).toLocaleDateString(locale, { weekday: "long" });
    if (n < 0 && n > -7) return `${-n} days ago`;
    return fmt.date(day, { month: "short", day: "numeric", year: parseDay(day).getFullYear() !== new Date().getFullYear() ? "numeric" : undefined });
  },
  // "today", "yesterday", "3 days ago" mid-sentence, but month names keep their capital.
  relativeLower: (day) => { const text = fmt.relativeDay(day); return /^[A-Z][a-z]+ \d/.test(text) ? `on ${text}` : text.toLowerCase(); },
  ago: (value) => {
    if (!value) return "";
    const t = typeof value === "number" ? value * 1000 : Date.parse(value);
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return "now";
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h`;
    if (s < 86400 * 7) return `${Math.floor(s / 86400)}d`;
    return fmt.date(new Date(t).toISOString().slice(0, 10));
  },
  // "just now", "5m ago", "3d ago", or a date for anything older than a week.
  since: (value) => { const a = fmt.ago(value); return a === "now" ? "just now" : /^\d+[mhd]$/.test(a) ? `${a} ago` : a; },
  clock: (value) => {
    const t = typeof value === "number" ? value * 1000 : Date.parse(value);
    const d = new Date(t);
    if (isoDay(d) === isoDay()) return fmt.time(value);
    if ((Date.now() - t) < 86400000 * 6) return d.toLocaleDateString(locale, { weekday: "short" });
    return d.toLocaleDateString(locale, { month: "short", day: "numeric" });
  },
  plural: (n, word, many = word + "s") => `${fmt.num(n)} ${n === 1 ? word : many}`,
};

// Motion ---------------------------------------------------------------------------------------------------------

// The notch's spring (0.42 s response, 0.82 damping) sampled into a CSS linear() easing, so web motion matches it.
export function springEasing(response = 0.42, damping = 0.82) {
  const omega = (2 * Math.PI) / response;
  const freq = omega * Math.sqrt(1 - damping * damping);
  const at = (t) => 1 - Math.exp(-damping * omega * t) * (Math.cos(freq * t) + (damping * omega / freq) * Math.sin(freq * t));
  let settle = 0.1;
  while (settle < 3 && !(Math.abs(at(settle) - 1) < 0.002 && Math.abs(at(settle + 0.05) - 1) < 0.002)) settle += 0.01;
  const points = [];
  for (let i = 0; i <= 48; i++) points.push(+at((settle * i) / 48).toFixed(4));
  points[points.length - 1] = 1;
  return { easing: `linear(${points.join(", ")})`, duration: Math.round(settle * 1000) };
}

export const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// One motion between two states of the page (another page, writing mode, a calendar view): elements that persist glide
// to their new place and size, the rest crossfades (glass.css → Motion). The browser first captures the current frame,
// which over heavy glass can take a few hundred milliseconds; the guard only covers a window that never paints (the
// overlay hidden), so the change still happens, unanimated.
export function transition(update, motion = "swap") {
  if (!document.startViewTransition || reducedMotion() || document.hidden) { update(); return Promise.resolve(); }
  const root = document.documentElement;
  root.dataset.motion = motion;  // picks the choreography in glass.css; set before the old state is captured
  let ran = false;
  const run = () => { if (!ran) { ran = true; update(); } };
  const vt = document.startViewTransition(run);
  const guard = setTimeout(() => { if (!ran) { vt.skipTransition(); run(); } }, 1500);
  vt.updateCallbackDone.catch(() => {}).finally(() => clearTimeout(guard));
  vt.ready.catch(() => {});  // a newer transition skipping this one isn't an error
  return vt.finished.catch(() => {}).finally(() => { if (root.dataset.motion === motion) delete root.dataset.motion; });
}

export function debounce(fn, ms) {
  let timer;
  const wrapped = (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
  wrapped.flush = (...args) => { clearTimeout(timer); fn(...args); };
  return wrapped;
}

export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
export const sum = (values) => values.reduce((a, b) => a + (b || 0), 0);
export const avg = (values) => { const v = values.filter((x) => x != null); return v.length ? sum(v) / v.length : null; };
