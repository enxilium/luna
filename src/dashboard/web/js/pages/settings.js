// Settings: the overlay shortcut and glass, your calendar and location, where messages come from, and you and your data.

import { h, api, fmt, settings, debounce } from "../core.js";
import { icon, pane, paneHead, button, segmented, pill, input, select, networkBadge, networkName, fail } from "../ui.js";
import { beeperDialog } from "./chat.js";
import { applyGlass, SHORTCUTS } from "../app.js";
import { native } from "../glass.js";

const PRIVACY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles";
const STATE = { ready: ["Connected", "good"], full_disk_access: ["Needs Full Disk Access", "attention"], not_configured: ["Not connected", null],
  not_running: ["Beeper isn't running", "attention"], token_rejected: ["Token rejected", "bad"], not_available: ["Unavailable", null] };

const CALENDAR_PRIVACY = "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars";
const CALENDAR = { ready: ["Connected", "good"], not_determined: ["Not connected", null], denied: ["Access is off", "attention"],
  write_only: ["Can't read events", "attention"], restricted: ["Blocked on this Mac", "attention"], unavailable: ["Unavailable", null] };
const LOCATION_PRIVACY = "x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices";
const LOCATION = { ready: ["On", "good"], not_determined: ["Off", null], denied: ["Access is off", "attention"],
  restricted: ["Blocked on this Mac", "attention"], off: ["Location Services are off", "attention"], unavailable: ["Unavailable", null] };

async function load() {
  const [values, status, calendar, location] = await Promise.all([api.get("/api/settings"),
    api.get("/api/messages/status").catch(() => ({ providers: [], beeper_configured: false })),
    api.get("/api/calendar").catch(() => ({ status: "unavailable" })), api.get("/api/location").catch(() => ({ status: "unavailable" }))]);
  return { values, status, calendar: calendar.status, location: location.status };
}

async function put(patch) {
  try { Object.assign(settings, await api.put("/api/settings", patch)); applyGlass(); } catch (e) { fail(e); }
}
const putSoon = debounce(put, 450);
const text = (key, props = {}) => input({ value: settings[key] ?? "", onInput: (e) => { settings[key] = e.target.value; putSoon({ [key]: e.target.value }); }, ...props });

function row(label, hint, control) {
  return h("div", { class: "setting", key: label }, h("div", { class: "setting-text" }, h("div", null, label), hint && h("div", { class: "faint" }, hint)), control);
}

export default {
  title: "Settings",
  events: ["messages"],
  cached: () => api.cached("/api/settings") && api.cached("/api/messages/status") && api.cached("/api/calendar") && api.cached("/api/location")
    ? { values: api.cached("/api/settings"), status: api.cached("/api/messages/status"), calendar: api.cached("/api/calendar").status,
      location: api.cached("/api/location").status } : null,
  load,
  render(data, ctx) {
    const v = settings;
    const networks = [...new Set(data.status.providers.flatMap((p) => p.networks || []))];
    const hidden = new Set(v.networks_hidden || []);
    return h("div", { class: "grid4 settings" },
      h("div", { class: "page-head span-all" }, h("div", null, h("h1", null, "Settings"),
        h("p", null, v.demo ? "Demo mode: changes last until you quit." : "Everything stays on this Mac."))),
      pane("main", { class: "span-2" }, h("div", { class: "pane-body" }, paneHead("Overlay", { sub: "How the dashboard opens and looks over your desktop" }),
        row("Shortcut", native ? "Shows or hides the dashboard from any app." : "Works in the Luna window, not in a browser.",
          select("overlay_shortcut", Object.entries(SHORTCUTS), v.overlay_shortcut || "ctrl+option+space",
            { class: "shortcut-select", onChange: (e) => { settings.overlay_shortcut = e.target.value; applyGlass(); put({ overlay_shortcut: e.target.value }); } })),
        row("Style", "Clear frosts less; Dark keeps text crisp over busy pictures.",
          segmented("glass-style", [{ value: "clear", label: "Clear" }, { value: "frosted", label: "Frosted" }, { value: "dark", label: "Dark" }],
            v.glass || "frosted", (style) => { settings.glass = style; applyGlass(); ctx.refresh(); put({ glass: style }); })),
        row("Backdrop", v.backdrop_folder ? `Pictures from ${v.backdrop_folder.split("/").filter(Boolean).pop()}, frosted like the glass.`
          : "Your desktop shows through. Choose a folder of pictures to use those instead.",
          h("div", { class: "setting-controls" },
            v.backdrop_folder ? button("Use desktop", { small: true, onClick: () => put({ backdrop_folder: "" }) }) : null,
            button(v.backdrop_folder ? "Change…" : "Choose folder…", { small: true, onClick: async () => {
              const r = await api.post("/api/pick", { kind: "folder", prompt: "Choose a folder of pictures" }).catch(fail);
              if (r && r.path) put({ backdrop_folder: r.path });
            } }))),
        v.backdrop_folder ? row("Next picture", null, select("backdrop_interval", [[1, "Every minute"], [5, "Every 5 minutes"], [10, "Every 10 minutes"],
          [30, "Every 30 minutes"], [60, "Every hour"]], v.backdrop_interval || 10,
          { class: "shortcut-select", onChange: (e) => put({ backdrop_interval: Number(e.target.value) }) })) : null,
        row("Tint", `${Math.round((v.glass_tint ?? 0.32) * 100)}%`, input({ type: "range", class: "input range tint-range", min: 0, max: 0.7, step: 0.02, value: v.glass_tint ?? 0.32,
          onInput: (e) => { settings.glass_tint = Number(e.target.value); applyGlass(); ctx.refresh(); putSoon({ glass_tint: Number(e.target.value) }); } })))),
      pane("side", { class: "span-2" }, h("div", { class: "pane-body" }, paneHead("Calendar & location", { sub: "Events and weather on Home" }),
        row("macOS Calendar", "Read-only. Covers every account synced to this Mac: Google, iCloud, Exchange.",
          h("div", { class: "setting-controls" }, pill(...(CALENDAR[data.calendar] || ["Unavailable", null])),
            data.calendar === "not_determined" && button("Allow", { small: true, onClick: async () => {
              try { await api.post("/api/calendar/access", {}); ctx.reload(); } catch (e) { fail(e); } } }),
            (data.calendar === "denied" || data.calendar === "write_only") && button("Open Settings", { small: true,
              onClick: () => api.post("/api/open", { url: CALENDAR_PRIVACY }).catch(fail) }))),
        row("Location", "For the weather on Home. Town-level; sent to Open-Meteo rounded to about 10 km.",
          h("div", { class: "setting-controls" }, pill(...(LOCATION[data.location] || ["Unavailable", null])),
            data.location === "not_determined" && button("Allow", { small: true, onClick: async () => {
              try { await api.post("/api/location/access", {}); ctx.reload(); } catch (e) { fail(e); } } }),
            (data.location === "denied" || data.location === "off") && button("Open Settings", { small: true,
              onClick: () => api.post("/api/open", { url: LOCATION_PRIVACY }).catch(fail) }))))),
      pane("bottom-1", { class: "span-2" }, h("div", { class: "pane-body" }, paneHead("Messages", { sub: "Sources for your inbox" }),
        data.status.providers.map((p) => {
          const [label, tone] = STATE[p.state] || [p.message || p.state, "attention"];
          return row(p.id === "imessage" ? "iMessage & SMS" : p.label, p.id === "imessage" ? "Read from Messages on this Mac; replies go out through Messages."
            : p.id === "beeper" ? "Instagram, LinkedIn, Discord and more through Beeper." : null,
          h("div", { class: "setting-controls" }, pill(label, tone),
            p.state === "full_disk_access" && button("Open Settings", { small: true, onClick: () => api.post("/api/open", { url: PRIVACY_URL }).catch(fail) }),
            p.id === "beeper" && button("Manage", { small: true, onClick: beeperDialog })));
        }),
        !data.status.beeper_configured && row("Instagram, LinkedIn & more", "Connect Beeper.", button("Connect", { small: true, onClick: beeperDialog })),
        networks.length ? row("Show in inbox", null, h("div", { class: "net-toggles" }, networks.map((n) => h("button", { type: "button", key: n,
          class: ["chip", !hidden.has(n) && "is-active"], onClick: () => { const next = new Set(hidden); next.has(n) ? next.delete(n) : next.add(n); put({ networks_hidden: [...next] }).then(ctx.reload); } },
        networkBadge(n), networkName(n))))) : null)),
      pane("bottom-2", { class: "span-2" }, h("div", { class: "pane-body" }, paneHead("You & your data", { sub: "Local only" }),
        row("Your name", "Greets you on Home.", text("name", { placeholder: "First Last", class: "input name-input" })),
        row("Setup", "Your Gemini key, the microphone and the wake word.", button("Run setup again", { small: true, onClick: () => ctx.go("setup") })),
        row("Stored at", null, h("span", { class: "mono faint path selectable" }, (v.database || "").replace(/^\/Users\/[^/]+/, "~"))),
        row("Privacy", "No account, nothing uploaded. Project folders and Messages are read, never changed.", null))));
  },
};
