// Messages live in glass sheets for now: an inbox and a conversation, both following the source apps live.

import { h, api, fmt, listen, isoDay } from "../core.js";
import { icon, iconButton, button, chip, avatar, networkBadge, networkName, empty, openLayer, sheet, field, input, fail, toast } from "../ui.js";

const PRIVACY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles";
const drafts = new Map();

export function openInbox(ctx) {
  let data = null, status = null, network = "all", query = "";
  const refresh = async () => {
    [data, status] = await Promise.all([api.get("/api/messages/chats").catch(() => ({ chats: [], problems: {} })),
      api.get("/api/messages/status").catch(() => ({ providers: [], beeper_configured: false }))]);
    layer.update();
  };
  const stop = listen((e) => { if (e.type === "messages") refresh(); });
  const layer = openLayer((close) => {
    const chats = data ? data.chats : [];
    const networks = [...new Set(chats.map((c) => c.network))];
    const q = query.trim().toLowerCase();
    const shown = chats.filter((c) => (network === "all" || c.network === network) && (!q || c.title.toLowerCase().includes(q) || (c.preview.text || "").toLowerCase().includes(q)));
    return sheet("Messages", close, [
      h("div", { class: "inbox-tools" },
        h("div", { class: "search-field grow" }, icon("magnifyingglass"), input({ value: query, placeholder: "Search conversations", autofocus: true,
          onInput: (e) => { query = e.target.value; layer.update(); } })),
        chip("All", { active: network === "all", onClick: () => { network = "all"; layer.update(); } }),
        networks.map((n) => chip(networkName(n), { key: n, active: network === n, onClick: () => { network = n; layer.update(); } }))),
      status && banners(status),
      h("div", { class: "inbox-list" }, !data ? h("div", { class: "empty" }, "Loading…") : shown.length ? shown.map((c) => h("button", {
        type: "button", class: ["row row-btn", c.unread && "is-unread"], key: c.id, onClick: () => { close(); openChat(c.id, ctx); } },
      h("span", { class: "avatar-stack" }, avatar(c.title, { size: 38 }), networkBadge(c.network)),
      h("div", { class: "row-main" }, h("div", { class: "row-title" }, c.title), h("div", { class: "row-sub" }, c.preview.from_me ? `You: ${c.preview.text}` : c.preview.text)),
      h("div", { class: "row-end" }, h("span", null, c.at ? fmt.clock(c.at) : ""), c.unread ? h("span", { class: "dot-count" }, c.unread) : null))) :
        empty("tray", chats.length ? "No matches" : "No conversations", chats.length ? null : "Connect a source below."))],
    null, { sub: data ? `${chats.reduce((n, c) => n + (c.unread || 0), 0)} unread across ${networks.length || 0} networks` : "" });
  }, { width: 560, onClose: () => stop() });
  refresh();
}

function banners(status) {
  const out = [];
  for (const p of status.providers) {
    if (p.state === "full_disk_access") {
      out.push(h("div", { class: "banner", key: p.id }, icon("lock.shield"), h("div", { class: "grow" }, h("b", null, "iMessage needs Full Disk Access"),
        h("div", { class: "faint" }, "Allow the app running Luna to read Messages.")),
      button("Open Settings", { small: true, onClick: () => api.post("/api/open", { url: PRIVACY_URL }).catch(fail) })));
    } else if (p.state !== "ready" && p.message) {
      out.push(h("div", { class: "banner", key: p.id }, icon("exclamationmark.triangle"), h("div", { class: "grow" }, p.message),
        p.id === "beeper" && button("Fix", { small: true, onClick: beeperDialog })));
    }
  }
  if (!status.beeper_configured) {
    out.push(h("button", { type: "button", class: "banner banner-soft", key: "beeper", onClick: beeperDialog },
      h("span", { class: "banner-nets" }, networkBadge("instagram"), networkBadge("linkedin"), networkBadge("whatsapp")),
      h("div", { class: "grow" }, h("b", null, "Add Instagram, LinkedIn and more"), h("div", { class: "faint" }, "Connect Beeper")),
      icon("chevron.right")));
  }
  return out;
}

export function beeperDialog() {
  openLayer((close) => sheet("Connect Beeper", close, [
    h("p", { class: "muted" }, "Beeper bridges Instagram, LinkedIn, WhatsApp, Discord and more, and stays in sync with them. Luna talks to Beeper Desktop or Beeper Server on this Mac."),
    h("ol", { class: "steps" }, h("li", null, "Run Beeper Desktop or Beeper Server and sign in to your accounts there."),
      h("li", null, "Desktop: Settings → Integrations → Approved connections → +, and create a token. Server: use the token `beeper setup` saved."),
      h("li", null, "Paste it here. It's kept in your Keychain.")),
    field("Access token", input({ name: "token", type: "password", autofocus: true, placeholder: "Paste token" }))],
  [button("Disconnect", { kind: "ghost", onClick: async () => { await api.put("/api/messages/beeper", { token: "" }).catch(fail); close(); } }),
    h("span", { class: "grow" }), button("Cancel", { onClick: () => close() }), button("Connect", { kind: "primary", type: "submit" })],
  { onSubmit: async (form) => {
    const token = new FormData(form).get("token");
    if (!token) return;
    try {
      const result = await api.put("/api/messages/beeper", { token });
      const beeper = result.providers.find((p) => p.id === "beeper");
      close();
      toast(beeper && beeper.state === "ready" ? "Beeper connected" : (beeper && beeper.message) || "Saved", beeper && beeper.state !== "ready" ? "bad" : null);
    } catch (e) { fail(e); }
  } }), { width: 480 });
}

export function openChat(chatId, ctx) {
  let chat = null, messages = null, error = null, pending = [], stick = true;
  const url = `/api/messages/chats/${encodeURIComponent(chatId)}`;
  const refresh = async () => {
    try {
      const [list, items] = await Promise.all([api.get("/api/messages/chats"), api.get(url)]);
      chat = list.chats.find((c) => c.id === chatId) || chat;
      messages = items; error = null;
      ctx && ctx.refreshUnread();
    } catch (e) { error = e.message; }
    layer.update();
  };
  const stop = listen((e) => { if (e.type === "messages") refresh(); });
  const send = async () => {
    const text = (drafts.get(chatId) || "").trim();
    if (!text) return;
    const temp = { id: `p${Date.now()}`, from_me: true, sender: "You", text, at: new Date().toISOString(), reactions: [], attachments: [], pending: true };
    pending.push(temp);
    drafts.set(chatId, "");
    const box = document.querySelector(".chat-input");
    if (box) box.value = "";
    stick = true;
    layer.update();
    try { await api.post(url, { text }); await refresh(); } catch (e) { fail(e); drafts.set(chatId, text); if (box) box.value = text; }
    finally { pending = pending.filter((m) => m !== temp); layer.update(); }
  };
  const layer = openLayer((close) => {
    const all = [...(messages || []), ...pending];
    return h("div", { class: "chat" },
      h("header", { class: "sheet-head" },
        chat && h("span", { class: "avatar-stack" }, avatar(chat.title, { size: 36 }), networkBadge(chat.network)),
        h("div", { class: "grow" }, h("h2", null, chat ? chat.title : "Conversation"), chat && h("div", { class: "pane-sub" }, networkName(chat.network))),
        iconButton("xmark", { title: "Close", onClick: () => close() })),
      h("div", { class: "chat-scroll", ref: (el) => { if (stick) requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; }); },
        onScroll: (e) => { const el = e.currentTarget; stick = el.scrollHeight - el.scrollTop - el.clientHeight < 60; } },
      error ? empty("exclamationmark.bubble", "Couldn't load messages", error) : messages == null ? h("div", { class: "empty" }, "Loading…") : bubbles(all, chat)),
      h("form", { class: ["chat-compose", (drafts.get(chatId) || "").trim() && "is-live"], onSubmit: (e) => { e.preventDefault(); send(); } },
        h("textarea", { class: "chat-input", rows: 1, placeholder: chat ? `Message on ${networkName(chat.network)}` : "Message", value: drafts.get(chatId) || "",
          autofocus: true, onInput: (e) => { drafts.set(chatId, e.target.value); e.target.style.height = "auto"; e.target.style.height = `${Math.min(140, e.target.scrollHeight)}px`;
            const live = !!e.target.value.trim(); e.target.form.classList.toggle("is-live", live); e.target.form.querySelector(".kbd").classList.toggle("is-live", live); },
          onKeydown: (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } } }),
        // The notch's Return keycap: outlined at rest, lit when there's something to send.
        h("button", { type: "submit", class: "kbd-btn", title: "Send", "aria-label": "Send" },
          h("span", { class: ["kbd", (drafts.get(chatId) || "").trim() && "is-live"] }, "⏎"))));
  }, { width: 560, class: "layer-chat", onClose: () => stop() });
  refresh();
}

function bubbles(messages, chat) {
  let lastDay = null;
  return h("div", { class: "bubbles" }, messages.map((m, i) => {
    const day = isoDay(new Date(m.at));
    const sep = day !== lastDay ? h("div", { class: "day-sep", key: `d${day}` }, fmt.relativeDay(day)) : null;
    lastDay = day;
    const next = messages[i + 1];
    const tail = !next || next.from_me !== m.from_me || next.sender !== m.sender;
    return [sep, h("div", { class: ["bubble-row", m.from_me ? "mine" : "theirs", tail && "tail", m.pending && "pending"], key: m.id },
      chat && chat.group && !m.from_me && tail ? h("div", { class: "bubble-sender" }, m.sender) : null,
      m.attachments.filter((a) => a.kind === "image" && a.url).map((a) => h("img", { class: "bubble-img", src: a.url, key: a.url, loading: "lazy" })),
      m.text && h("div", { class: "bubble selectable", "data-tip": fmt.time(m.at) }, m.text),
      m.reactions.length ? h("div", { class: "reactions" }, m.reactions.map((r, k) => h("span", { key: String(k) }, r.emoji))) : null)];
  }));
}
