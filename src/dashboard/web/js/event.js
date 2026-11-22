// An event, opened in Home's sheet. Your own events open ready to edit; an invitation opens to answer: who invited you,
// Going / Maybe / Can't go, and who else is coming. Times are local ("2026-11-19T18:00"); an all-day event runs from
// midnight on its first day to midnight after its last, like the calendars it replaces.

import { h, api, fmt, go, isoDay, addDays, parseDay } from "./core.js";
import { icon, iconButton, button, avatar, fail, confirmDialog } from "./ui.js";
import { hue, ownerChip, courseOwner } from "./work.js";

const RSVP = [["accepted", "Going"], ["maybe", "Maybe"], ["declined", "Can't go"]];
const GUEST = { accepted: ["Going", "is-going"], maybe: ["Maybe", "is-maybe"], declined: ["Declined", "is-declined"], pending: ["No reply yet", "is-pending"] };
const pad = (n) => String(n).padStart(2, "0");
const hhmm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

// A draft keeps the form's values between renders: Home re-renders on every change elsewhere.
let draft = null;

function blank(day, courseId) {
  const now = new Date(), start = new Date(now);
  start.setMinutes(0, 0, 0);
  start.setHours(now.getHours() + 1);
  const date = day || isoDay(start);
  return { id: null, title: "", date, end_date: date, start: hhmm(start), end: hhmm(new Date(start.getTime() + 3600e3)), all_day: false,
    location: "", description: "", important: false, course_id: courseId || null, attendees: [], dirty: false };
}

function fromEvent(e) {
  const a = new Date(e.start), b = new Date(e.end);
  const last = e.all_day ? addDays(b, -1) : b;
  return { id: e.id, title: e.title, date: isoDay(a), end_date: isoDay(last), start: hhmm(a), end: hhmm(b), all_day: e.all_day,
    location: e.location, description: e.description, important: e.important, course_id: e.course_id, attendees: e.attendees || [], dirty: false };
}

function payload(f) {
  const start_at = f.all_day ? `${f.date}T00:00` : `${f.date}T${f.start}`;
  const end_at = f.all_day ? `${isoDay(addDays(parseDay(f.end_date < f.date ? f.date : f.end_date), 1))}T00:00`
    : `${f.date}T${f.end < f.start ? f.start : f.end}`;
  return { title: f.title.trim(), start_at, end_at, all_day: f.all_day, location: f.location.trim(), description: f.description.trim(),
    important: f.important, course_id: f.course_id || null, attendees: f.attendees };
}

export async function loadEvent(params) {
  if (params[0] !== "event") { draft = null; return null; }
  const [id, query = ""] = (params[1] || "").split("?");
  const q = new URLSearchParams(query);
  const owners = await api.get("/api/owners").catch(() => ({ courses: [] }));
  if (id === "new") {
    if (!draft || draft.id !== null || draft.fresh !== params[1]) draft = { ...blank(q.get("date"), q.get("course")), fresh: params[1] };
    return { event: null, owners };
  }
  const event = await api.get(`/api/events/${id}`);
  if (!draft || draft.id !== event.id || !draft.dirty) draft = fromEvent(event);
  return { event, owners };
}

const close = () => { draft = null; go("home"); };

export function eventPanel(x, ctx) {
  const e = x.event;
  if (e && e.rsvp) return invitation(e, x, ctx);
  return editor(e, x, ctx);
}

function guests(list, editable) {
  return list.map((a, i) => {
    const [label, cls] = a.organizer ? ["Organizer", "is-organizer"] : GUEST[a.rsvp] || GUEST.pending;
    return h("div", { key: `${a.email || a.name}-${i}`, class: "guest" }, avatar(a.name || a.email, { size: 28 }),
      h("div", { class: "guest-main" }, h("span", { class: "guest-name" }, a.name || a.email), a.name && a.email ? h("span", { class: "meta" }, a.email) : null),
      editable ? iconButton("xmark", { small: true, title: `Remove ${a.name || a.email}`, onClick: () => { draft.attendees = draft.attendees.filter((_, n) => n !== i); draft.dirty = true; editable(); } })
        : h("span", { class: ["guest-rsvp", cls] }, a.rsvp === "accepted" && !a.organizer ? icon("checkmark.circle.fill") : null, label));
  });
}

function whenText(e) {
  const a = new Date(e.start), b = new Date(e.end);
  const day = a.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  if (e.all_day) return [day, "All day"];
  return [day, `${fmt.time(e.start)} – ${fmt.time(e.end)}`];
}

function invitation(e, x, ctx) {
  const course = (x.owners.courses || []).find((c) => c.id === e.course_id);
  const answer = async (rsvp) => { try { await api.post(`/api/events/${e.id}/rsvp`, { rsvp }); ctx.reload(); } catch (err) { fail(err); } };
  const [day, time] = whenText(e);
  const others = (e.attendees || []);
  const counts = ["accepted", "maybe", "declined"].map((k) => [k, others.filter((a) => a.rsvp === k).length]).filter(([, n]) => n);
  return h("aside", { class: "pane home-sheet event-panel", "data-pane": "home-sheet", "data-glass": "panel", key: "home-sheet" },
    h("header", { class: "event-bar" }, course ? ownerChip(courseOwner(course)) : h("span", { class: "meta" }, "Invitation"), h("span", { class: "grow" }),
      iconButton("xmark", { title: "Close", onClick: close })),
    h("h1", { class: "event-title-lg" }, e.title),
    h("div", { class: "event-facts" },
      fact("clock", day, time), e.location ? fact("mappin.and.ellipse", e.location) : null, e.description ? fact("text.alignleft", null, e.description) : null),
    h("section", { class: ["rsvp-box", e.rsvp === "pending" && "is-waiting"] },
      h("div", { class: "rsvp-head" }, avatar(e.organizer ? e.organizer.name || e.organizer.email : "?", { size: 28 }),
        h("div", { class: "grow" }, h("b", null, `${e.organizer ? e.organizer.name || e.organizer.email : "Someone"} invited you`),
          h("span", { class: "meta" }, e.rsvp === "pending" ? "Waiting for your answer" : `You answered ${RSVP.find(([k]) => k === e.rsvp)[1].toLowerCase()}`))),
      h("div", { class: "rsvp-buttons", role: "radiogroup" }, RSVP.map(([k, label]) => h("button", { type: "button", key: k, role: "radio",
        "aria-checked": String(e.rsvp === k), class: ["btn", "btn-plain", e.rsvp === k && "is-on"], onClick: () => e.rsvp !== k && answer(k) }, label)))),
    others.length ? h("section", { class: "event-guests" },
      h("header", { class: "sec-row" }, h("h2", null, "Guests"), h("span", { class: "meta" }, counts.map(([k, n]) => `${n} ${GUEST[k][0].toLowerCase()}`).join(" · "))),
      guests(others, null)) : null);
}

function fact(symbol, strong, soft) {
  return h("div", { class: "fact" }, icon(symbol), h("div", null, strong ? h("div", { class: "fact-strong" }, strong) : null, soft ? h("div", { class: "fact-soft" }, soft) : null));
}

function editor(e, x, ctx) {
  const f = draft;
  const set = (patch) => { Object.assign(f, patch, { dirty: true }); ctx.refresh(); };
  const save = async () => {
    if (!f.title.trim()) { fail(new Error("Give the event a name.")); return; }
    try {
      f.id ? await api.patch(`/api/events/${f.id}`, payload(f)) : await api.post("/api/events", payload(f));
      draft = null;
      go("home");
    } catch (err) { fail(err); }
  };
  const addGuest = (value) => {
    const text = value.trim();
    if (!text) return;
    const email = /\S+@\S+\.\S+/.exec(text)?.[0] || "";
    const name = text.replace(email, "").replace(/[<>]/g, "").trim();
    f.attendees = [...f.attendees, { name, email, rsvp: "pending" }];
    f.dirty = true;
    ctx.refresh();
  };
  const courses = x.owners.courses || [];
  const course = courses.find((c) => c.id === f.course_id);
  return h("aside", { class: "pane home-sheet event-panel", "data-pane": "home-sheet", "data-glass": "panel", key: "home-sheet" },
    h("header", { class: "event-bar" },
      h("select", { class: "course-select", style: course ? hue(course.color) : null, "aria-label": "Course", onChange: (ev) => set({ course_id: ev.target.value || null }) },
        h("option", { value: "", selected: !f.course_id }, "No course"), courses.map((c) => h("option", { value: c.id, selected: c.id === f.course_id }, c.code))),
      h("span", { class: "grow" }),
      h("button", { type: "button", class: ["icon-btn", "star-btn", f.important && "is-on"], "aria-pressed": String(f.important),
        title: f.important ? "Important: highlighted on every calendar view" : "Mark as important", onClick: () => set({ important: !f.important }) },
      icon(f.important ? "star.fill" : "star")),
      iconButton("xmark", { title: "Close", onClick: close })),
    h("input", { class: "event-title-input", value: f.title, placeholder: "New event", autofocus: !f.id, "aria-label": "Event name",
      onInput: (ev) => { f.title = ev.target.value; f.dirty = true; }, onKeydown: (ev) => { if (ev.key === "Enter") save(); } }),
    h("div", { class: "event-form" },
      h("div", { class: "fact" }, icon("clock"), h("div", { class: "fact-fields" },
        h("div", { class: "field-line" },
          h("input", { type: "date", class: "input", value: f.date, "aria-label": "Date", onChange: (ev) => set({ date: ev.target.value, end_date: f.end_date < ev.target.value ? ev.target.value : f.end_date }) }),
          f.all_day ? [h("span", { class: "meta", key: "to" }, "to"), h("input", { key: "end-date", type: "date", class: "input", value: f.end_date, min: f.date, "aria-label": "Last day", onChange: (ev) => set({ end_date: ev.target.value }) })] : null),
        f.all_day ? null : h("div", { class: "field-line" },
          h("input", { type: "time", class: "input", value: f.start, "aria-label": "Starts", onChange: (ev) => {
            const [a, b] = [f.start, ev.target.value].map((v) => Number(v.slice(0, 2)) * 60 + Number(v.slice(3)));
            const end = Number(f.end.slice(0, 2)) * 60 + Number(f.end.slice(3)) + (b - a);
            set({ start: ev.target.value, end: end < 1440 ? `${pad(Math.floor(end / 60))}:${pad(end % 60)}` : "23:59" });
          } }),
          h("span", { class: "meta" }, "to"),
          h("input", { type: "time", class: "input", value: f.end, "aria-label": "Ends", onChange: (ev) => set({ end: ev.target.value }) })),
        h("label", { class: "switch-line" }, h("button", { type: "button", role: "switch", class: ["switch", f.all_day && "is-on"], "aria-checked": String(f.all_day),
          onClick: () => set({ all_day: !f.all_day }) }, h("i")), h("span", null, "All day")))),
      h("div", { class: "fact" }, icon("mappin.and.ellipse"), h("input", { class: "input", value: f.location, placeholder: "Add a place", "aria-label": "Location",
        onInput: (ev) => { f.location = ev.target.value; f.dirty = true; } })),
      h("div", { class: "fact" }, icon("text.alignleft"), h("textarea", { class: "input textarea", rows: 2, value: f.description, placeholder: "Add a note", "aria-label": "Note",
        onInput: (ev) => { f.description = ev.target.value; f.dirty = true; } }))),
    h("section", { class: "event-guests" },
      h("header", { class: "sec-row" }, h("h2", null, "Guests"), f.attendees.length ? h("span", { class: "meta" }, String(f.attendees.length)) : null),
      h("div", { class: "fact" }, icon("person.badge.plus"), h("input", { class: "input", placeholder: "Add people by name or email", "aria-label": "Add a guest",
        onKeydown: (ev) => { if (ev.key === "Enter" && ev.target.value.trim()) { ev.preventDefault(); addGuest(ev.target.value); ev.target.value = ""; } } })),
      guests(f.attendees, () => ctx.refresh())),
    h("footer", { class: "event-foot" },
      f.id ? button("Delete", { kind: "danger", onClick: async () => {
        if (!(await confirmDialog("Delete this event?", `“${f.title || "Untitled"}” comes off your calendar.`))) return;
        try { await api.del(`/api/events/${f.id}`); close(); } catch (err) { fail(err); }
      } }) : null,
      h("span", { class: "grow" }),
      button("Cancel", { onClick: close }),
      button(f.id ? "Save" : "Add event", { kind: "primary", onClick: save })));
}
