// First-run setup: what Luna needs before it can listen (a Gemini key, the microphone, the wake word model), then the
// optional permissions and connections (memory, Google, Spotify, Canvas), then how to call it. Luna opens here by itself
// until voice is ready; Settings can bring it back.

import { h, api, settings, debounce, go } from "../core.js";
import { icon, button, pill, input, fail } from "../ui.js";

const KEY_URL = "https://aistudio.google.com/apikey";
const MIC_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone";
const FILES_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles";
const CALENDAR_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars";
const LOCATION_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices";
const STEPS = [["welcome", "Welcome"], ["key", "Gemini"], ["microphone", "Microphone"], ["wake", "Wake word"],
  ["extras", "Extras"], ["connect", "Connections"], ["done", "Ready"]];
const OPTIONAL = new Set(["extras", "connect"]);
const KEY_ERRORS = { invalid_key: "Google didn't accept that key. Check it was copied whole.",
  unreachable: "Luna couldn't reach Google. Check your connection and try again.",
  keychain_unavailable: "The key couldn't be saved to your Keychain." };

// The flow's own state survives re-renders (status updates arrive while you type).
const ui = { step: "welcome", key: "", checking: false, keyError: null, changingKey: false, asking: null, focused: null,
  canvas: { open: false, address: "", token: "" } };
// A step's field takes focus when the step appears (autofocus only works on a page's first load), so Enter goes on.
const focusOnArrival = (el) => { if (el && ui.focused !== ui.step) { ui.focused = ui.step; requestAnimationFrame(() => el.focus()); } };
const saveName = debounce((name) => api.put("/api/settings", { name }).catch(fail), 400);

async function load() {
  const [status, calendar, location, messages] = await Promise.all([api.get("/api/setup"),
    api.get("/api/calendar").catch(() => ({})), api.get("/api/location").catch(() => ({})),
    api.get("/api/messages/status").catch(() => ({ providers: [] }))]);
  // The model is needed and harmless to fetch: start it at once so it's usually done by the time you get there.
  if (status.wake_model.state === "idle") api.post("/api/setup/wake-model", {}).catch(() => {});
  return { status, calendar: calendar.status, location: location.status,
    imessage: (messages.providers || []).find((p) => p.id === "imessage") };
}

// Optional extras count as done once you've gone past them.
const passed = (id) => STEPS.findIndex(([s]) => s === ui.step) > STEPS.findIndex(([s]) => s === id);
const done = (d, id) => ({ welcome: !!settings.name, key: d.status.key, microphone: d.status.microphone === "granted",
  wake: d.status.wake_model.state === "done", extras: passed("extras"), connect: passed("connect"), done: false })[id];

function steps(d, ctx) {
  const at = STEPS.findIndex(([id]) => id === ui.step);
  return h("ol", { class: "setup-steps" }, STEPS.map(([id, label], i) => h("li", { key: id },
    h("button", { type: "button", class: ["setup-step", id === ui.step && "is-current", done(d, id) && "is-done"],
      disabled: i > at && !STEPS.slice(0, i).every(([prev]) => OPTIONAL.has(prev) || done(d, prev)),
      onClick: () => { ui.step = id; ctx.refresh(); } },
    h("span", { class: "setup-dot" }, done(d, id) && id !== ui.step ? icon("checkmark", { weight: "bold" }) : null), label))));
}

const next = (ctx) => { ui.step = STEPS[STEPS.findIndex(([id]) => id === ui.step) + 1][0]; ctx.refresh(); };
const actions = (...items) => h("div", { class: "setup-actions" }, items);
const open = (url) => api.post("/api/open", { url }).catch(fail);

function welcome(d, ctx) {
  return [h("h1", null, "Hi, I'm Luna."),
    h("p", { class: "setup-lede" }, "I live in your notch and stay out of the way until you call me."),
    h("label", { class: "setup-field" }, h("span", null, "What should I call you?"),
      input({ value: settings.name || "", placeholder: "Your first name", ref: focusOnArrival,
        onInput: (e) => { settings.name = e.target.value; saveName(e.target.value); },
        onKeydown: (e) => { if (e.key === "Enter") next(ctx); } })),
    actions(button("Continue", { kind: "primary", onClick: () => next(ctx) }))];
}

async function saveKey(ctx) {
  if (!ui.key.trim() || ui.checking) return;
  ui.checking = true; ui.keyError = null; ctx.refresh();
  try {
    const { result } = await api.post("/api/setup/key", { key: ui.key });
    if (result === "ok") { ui.key = ""; ui.changingKey = false; await ctx.reload(); next(ctx); } else ui.keyError = KEY_ERRORS[result] || KEY_ERRORS.unreachable;
  } catch (e) { ui.keyError = e.message; }
  if (ui.keyError) ui.focused = null;  // back to the field (it was disabled while checking), to fix the key
  ui.checking = false; ctx.refresh();
}

function key(d, ctx) {
  const head = [h("h1", null, "Connect Gemini"),
    h("p", { class: "setup-lede" }, "Luna thinks and talks with Google's Gemini, using your own API key. Getting one is free; Google bills only past its free tier.")];
  if (d.status.key && !ui.changingKey) {
    return [...head, h("div", { class: "setup-status" }, pill("Key saved", "good"), h("span", null, "Kept in your Keychain, on this Mac.")),
      actions(button("Continue", { kind: "primary", onClick: () => next(ctx) }),
        button("Use another key", { onClick: () => { ui.changingKey = true; ctx.refresh(); } }))];
  }
  return [...head,
    h("ol", { class: "setup-howto" }, h("li", null, "Open Google AI Studio and sign in with your Google account."),
      h("li", null, "Choose Create API key, then copy it."), h("li", null, "Paste it here.")),
    h("label", { class: "setup-field" }, h("span", null, "API key"),
      input({ type: "password", value: ui.key, placeholder: "Paste your key", ref: focusOnArrival, disabled: ui.checking,
        onInput: (e) => { ui.key = e.target.value; ui.keyError = null; },
        onKeydown: (e) => { if (e.key === "Enter") saveKey(ctx); } })),
    ui.keyError ? h("p", { class: "setup-error" }, ui.keyError) : null,
    actions(button(ui.checking ? "Checking…" : "Check and save", { kind: "primary", disabled: ui.checking, onClick: () => saveKey(ctx) }),
      button("Open AI Studio", { icon: "arrow.up.right", onClick: () => open(KEY_URL) }))];
}

async function askMicrophone(ctx) {
  ui.asking = "microphone"; ctx.refresh();
  try { await api.post("/api/setup/microphone", {}); await ctx.reload(); } catch (e) { fail(e); }
  ui.asking = null; ctx.refresh();
}

function microphone(d, ctx) {
  const state = d.status.microphone;
  return [h("h1", null, "Let Luna hear you"),
    h("p", { class: "setup-lede" }, "Luna listens for its name on this Mac. Your voice only goes to Gemini after you say “Luna”, until the conversation ends."),
    state === "granted" ? h("div", { class: "setup-status" }, pill("Allowed", "good"))
      : state === "denied" || state === "restricted" ? h("div", { class: "setup-status" }, pill("Turned off", "bad"),
        h("span", null, "Turn Luna on under Privacy & Security → Microphone."))
        : null,
    actions(
      state === "granted" ? button("Continue", { kind: "primary", onClick: () => next(ctx) })
        : state === "undetermined" ? button(ui.asking ? "Waiting for your answer…" : "Allow microphone", { kind: "primary", disabled: !!ui.asking, onClick: () => askMicrophone(ctx) })
          : button("Open Settings", { kind: "primary", onClick: () => open(MIC_SETTINGS) }),
      state !== "granted" ? button("Not now", { onClick: () => next(ctx) }) : null)];
}

function wake(d, ctx) {
  const m = d.status.wake_model, share = m.total ? Math.min(1, m.done / m.total) : 0;
  return [h("h1", null, "Teach Luna its name"),
    h("p", { class: "setup-lede" }, "A small model hears “Luna” right on this Mac, without the internet. It's downloaded once."),
    m.state === "done" ? h("div", { class: "setup-status" }, pill("Ready", "good"))
      : m.state === "failed" ? h("p", { class: "setup-error" }, "The download didn't finish. Check your connection and try again.")
        : h("div", { class: "setup-progress", role: "progressbar", "aria-valuenow": Math.round(share * 100), "aria-valuemin": 0, "aria-valuemax": 100 },
          h("div", { class: "setup-bar" }, h("i", { style: { width: `${Math.max(4, share * 100)}%` } })),
          h("span", null, m.total ? `${(m.done / 1e6).toFixed(1)} of ${(m.total / 1e6).toFixed(1)} MB` : "Starting…")),
    actions(m.state === "failed" ? button("Try again", { kind: "primary", onClick: async () => { await api.post("/api/setup/wake-model", {}).catch(fail); ctx.reload(); } })
      : button("Continue", { kind: "primary", disabled: m.state !== "done", onClick: () => next(ctx) }))];
}

function extra(label, hint, state, control) {
  return h("div", { class: "setup-extra", key: label }, h("div", { class: "setup-extra-text" }, h("div", null, label), h("span", null, hint)),
    h("div", { class: "setup-extra-control" }, state, control));
}

// One permission's state and the one thing to do about it: Allow while macOS hasn't asked, Open Settings once it's off.
function permission(state, path, privacy, ctx) {
  if (state === "ready") return [pill("On", "good"), null];
  if (state === "not_determined") return [null, button("Allow", { small: true, onClick: async () => { await api.post(path, {}).catch(fail); ctx.reload(); } })];
  if (["denied", "write_only", "off"].includes(state)) return [pill("Off", "attention"), button("Open Settings", { small: true, onClick: () => open(privacy) })];
  return [pill("Unavailable", null), null];
}

function extras(d, ctx) {
  const files = d.imessage && d.imessage.state;
  return [h("h1", null, "A little more, if you like"),
    h("p", { class: "setup-lede" }, "Each of these adds to the dashboard. Skip any of them; Settings has them all later."),
    h("div", { class: "setup-extras" },
      extra("Calendar", "Your events on Home's timeline. Read-only.", ...permission(d.calendar, "/api/calendar/access", CALENDAR_SETTINGS, ctx)),
      extra("Location", "The weather where you are, at town level.", ...permission(d.location, "/api/location/access", LOCATION_SETTINGS, ctx)),
      d.imessage ? extra("Messages", "Your iMessage conversations in Luna's inbox. Needs Full Disk Access.",
        files === "ready" ? pill("On", "good") : null,
        files === "full_disk_access" ? button("Open Settings", { small: true, onClick: () => open(FILES_SETTINGS) }) : null) : null),
    actions(button("Continue", { kind: "primary", onClick: () => next(ctx) }))];
}

// Connections -------------------------------------------------------------------------------------------------------

const CONNECTIONS = [
  ["memory", "Memory", "Luna remembers what matters across conversations, privately on this Mac. Downloads about 18 GB, once."],
  ["google", "Google", "Gmail, Calendar, Drive, Docs, Sheets, Slides, Chat and Contacts. You sign in once, in your browser."],
  ["spotify", "Spotify", "Find and play music on your devices. You sign in once, in your browser."],
  ["canvas", "Canvas", "Courses, assignments, quizzes and announcements from your school's Canvas, read-only."],
];
const CONNECT_ERRORS = {
  disk_full: "Memory needs about 22 GB free on this Mac.",
  download_failed: "The download didn't finish. Check your connection and try again.",
  model_damaged: "Memory's model in this copy of Luna is damaged. Try again, or download a fresh copy of Luna.",
  service_unavailable: "Memory didn't start. Try again; if it keeps failing, restart your Mac and try once more.",
  tools_missing: "This copy of Luna is missing the tools for this.",
  install_failed: "Setup didn't finish. Check your connection and try again.",
  sign_in_cancelled: "Sign-in was cancelled.",
  sign_in_timed_out: "Sign-in took too long. Try again when you're ready.",
  browser_unavailable: "Luna couldn't open your browser.",
  token_rejected: "Canvas didn't accept that token. Check the address and the token.",
  token_missing: "Paste your Canvas token first.",
  canvas_address_invalid: "That doesn't look like a Canvas address.",
  keychain_access_required: "macOS didn't let Luna into the Keychain. Try again and choose Always Allow.",
  keychain_unavailable: "Luna couldn't use the Keychain. Try again.",
};
const CANVAS_TOKEN_STEPS = "In Canvas: Account → Settings → Approved Integrations → New Access Token. Copy the token it shows.";

async function connect(name, ctx, options = {}) {
  if (name === "canvas") { ui.canvas.open = false; ui.canvas.token = ""; }  // the row shows progress, then the outcome
  try { await api.post(`/api/setup/connect/${name}`, options); } catch (e) { fail(e); }
  ctx.reload();
}

function canvasForm(ctx) {
  const c = ui.canvas;
  return h("div", { class: "setup-connect-form" },
    h("p", { class: "setup-hint" }, CANVAS_TOKEN_STEPS),
    h("label", { class: "setup-field" }, h("span", null, "Your school's Canvas address"),
      input({ value: c.address, placeholder: "q.utoronto.ca", onInput: (e) => { c.address = e.target.value; } })),
    h("label", { class: "setup-field" }, h("span", null, "Access token"),
      input({ type: "password", value: c.token, placeholder: "Paste your token", onInput: (e) => { c.token = e.target.value; },
        onKeydown: (e) => { if (e.key === "Enter") connect("canvas", ctx, { address: c.address, token: c.token }); } })),
    actions(button("Connect Canvas", { kind: "primary", onClick: () => connect("canvas", ctx, { address: c.address, token: c.token }) }),
      button("Cancel", { onClick: () => { c.open = false; ctx.refresh(); } })));
}

function connection(d, ctx, [name, label, hint]) {
  const c = d.status.connections[name];
  let state = null, control = null, detail = null;
  if (!c.available) state = pill("Not in this copy", null);
  else if (c.state === "done") state = pill("Connected", "good");
  else if (c.state === "running") {
    const share = c.total ? Math.min(1, c.done / c.total) : 0;
    detail = h("div", { class: "setup-progress" },
      c.total ? h("div", { class: "setup-bar" }, h("i", { style: { width: `${Math.max(2, share * 100)}%` } })) : null,
      h("span", null, (c.step || "Starting") + (c.total ? ` · ${(c.done / 1e9).toFixed(1)} of ${(c.total / 1e9).toFixed(1)} GB` : "…")));
  } else if (name === "canvas" && ui.canvas.open) detail = canvasForm(ctx);
  else {
    control = button(c.state === "failed" ? "Try again" : name === "memory" ? "Set up" : "Connect", { small: true,
      onClick: () => { if (name === "canvas") { ui.canvas.open = true; ctx.refresh(); } else connect(name, ctx); } });
  }
  if (c.state === "failed" && !(name === "canvas" && ui.canvas.open)) {
    detail = h("p", { class: "setup-error" }, CONNECT_ERRORS[c.error] || CONNECT_ERRORS.install_failed);
  }
  return h("div", { class: "setup-connection", key: name },
    extra(label, hint, state, control), detail);
}

function connections(d, ctx) {
  return [h("h1", null, "Connect what you use"),
    h("p", { class: "setup-lede" }, "Luna can remember, read your mail and calendar, play music and keep up with your courses. Each is optional; set up any of them now or later."),
    h("div", { class: "setup-extras" }, CONNECTIONS.map((item) => connection(d, ctx, item))),
    actions(button("Continue", { kind: "primary", onClick: () => next(ctx) }))];
}

function ready(d) {
  const shortcut = { "ctrl+option+space": "⌃⌥Space", "option+space": "⌥Space", "cmd+shift+space": "⇧⌘Space",
    "ctrl+option+l": "⌃⌥L", "cmd+shift+l": "⇧⌘L" }[settings.overlay_shortcut] || "⌃⌥Space";
  const missing = !d.status.voice_ready;
  return [h("h1", null, settings.name ? `You're all set, ${settings.name.split(/\s+/)[0]}.` : "You're all set."),
    h("p", { class: "setup-lede" }, missing ? "Luna still needs a Gemini key and its wake word model before it can listen." : "Luna is listening for its name. Three ways to reach it:"),
    missing ? null : h("ul", { class: "setup-ways" },
      h("li", null, h("span", { class: "setup-way-key" }, "“Luna”"), h("span", null, "Say it, then ask anything.")),
      h("li", null, h("span", { class: "setup-way-key" }, "Notch"), h("span", null, "Point at the notch to type instead.")),
      h("li", null, h("span", { class: "setup-way-key" }, shortcut), h("span", null, "Opens this dashboard, from any app."))),
    actions(button("Start using Luna", { kind: "primary", disabled: missing, onClick: () => go("home") }))];
}

const BODIES = { welcome, key, microphone, wake, extras, connect: connections, done: ready };

export default {
  title: "Setup",
  events: ["setup", "settings"],
  load,
  render(d, ctx) {
    return h("div", { class: "setup" },
      steps(d, ctx),
      h("section", { class: "pane setup-card", "data-pane": "main", "data-glass": "panel", key: `setup-${ui.step}` },
        h("div", { class: "setup-body" }, BODIES[ui.step](d, ctx))));
  },
  // Opening setup again (from Settings) starts at the first thing still missing.
  start(d) {
    ui.focused = null;
    ui.step = !d.status.key ? "key" : d.status.wake_model.state !== "done" ? "wake" : "welcome";
    if (!settings.name) ui.step = "welcome";
  },
};
