"""Quercus (Canvas) kept in step with Luna's courses, read through the read-only canvas-mcp integration.

Deterministic: no model is involved. For every active Luna course that matches a Canvas course by code (CSC311H1F
20269 → CSC311) the sync reads assignments (graded quizzes and New Quizzes are assignments in Canvas), Classic quizzes
that aren't (practice quizzes, surveys), submission state, and announcements, then:

- keeps one task per dated item: new ones are created, a moved deadline moves the task, and an item Canvas reports as
  submitted (or graded, or excused) is marked done the moment it changes to that. Titles, descriptions and anything
  else you edit stay yours; a task you reopen stays open until Canvas reports a new change.
- mirrors announcements into the course: new and edited ones are written, deleted ones disappear.

Canvas never gets written to. Names and bodies are instructor-authored text: they are stored and shown, never acted on.
"""

import asyncio
from datetime import date, datetime, timedelta
import json
import re
import time
from zoneinfo import ZoneInfo

from loguru import logger

INTEGRATION = "canvas"


def _site():
    """The school's Canvas address and timezone, from the Canvas integration setup registered (Quercus and Toronto)."""
    from src.install.canvas import site

    address, zone = site()
    return address, ZoneInfo(zone)
CODE = re.compile(r"^([A-Z]{3}\d{3})")
INLINE = re.compile(r"<<<UNTRUSTED CANVAS CONTENT \([^)]*\): (.*?)>>>", re.S)
BLOCK = re.compile(r"<<<UNTRUSTED CANVAS CONTENT \([^)]*\) — [^\n]*>>>\n(.*?)\n<<<END UNTRUSTED CANVAS CONTENT>>>", re.S)
SHORT = re.compile(r"^([a-z]{1,3})\s*#?\s*(\d{1,2})\b")  # "A3", "PA 2", "Lab 4", "Assignment 3" → (a, 3)
WORDS = {"assignment": "a", "asst": "a", "homework": "hw", "problem set": "ps", "project": "p", "proj": "p", "quiz": "q",
         "deliverable": "d", "milestone": "m", "exercise": "e", "lab": "lab", "test": "t", "midterm": "mt"}
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2})")
ANNOUNCEMENT_DAYS = 150  # one term and a bit: the window read (and pruned) each sync
DONE_STATES = {"submitted"}


class SyncError(RuntimeError):
    """A reason code (auth_required, integration_unavailable, …), never Canvas text."""


def _blocks(text):
    return [b for b in re.split(r"\n\s*\n", text) if b.strip()]


def _fields(block):
    out = {}
    for line in block.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key.strip() in ("Code", "Name", "ID", "Due", "Points"):
            out[key.strip()] = value.strip()
    return out


def _unfence(value):
    match = INLINE.search(value or "")
    return " ".join((match.group(1) if match else value or "").split())


def _norm(title):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).split())


def parse_courses(text):
    """`list_courses` output → [{"id", "code"}] for courses whose code starts like CSC311."""
    out = []
    for block in _blocks(text):
        f = _fields(block)
        match = CODE.match(f.get("Code", ""))
        if match and f.get("ID", "").isdigit():
            out.append({"id": f["ID"], "code": match.group(1)})
    return out


def parse_assignments(text):
    """`list_assignments(raw_dates=True)` output → [{"id", "name", "due_at"}]. Dates come from the JSON block, which
    keeps Canvas's own values (and a section's date when the assignment has no base one)."""
    body, _, raw = text.partition("Raw dates (JSON")
    dates = {}
    if raw:
        try:
            payload = json.loads(raw[raw.index("{"):])
        except ValueError:
            payload = {}
        for entry in payload.get("assignments", []):
            due = entry.get("due_at") or next((d.get("due_at") for d in entry.get("all_dates") or [] if d.get("due_at")), None)
            dates[str(entry.get("assignment_id"))] = due
    out = []
    for block in _blocks(body):
        f = _fields(block)
        if not f.get("ID", "").isdigit() or "Name" not in f:
            continue
        due = dates.get(f["ID"])
        if due is None and ISO.fullmatch(f.get("Due", "")):
            due = f["Due"]
        out.append({"id": f["ID"], "name": _unfence(f["Name"])[:200], "due_at": due})
    return out


def parse_quizzes(text):
    """The Classic section of `list_quizzes` → [{"id", "name", "due_at", "submitted"}]. New Quizzes are skipped: Canvas
    lists them as assignments already."""
    classic = text.split("\nNew Quizzes", 1)[0]
    out = []
    for chunk in re.split(r"\n(?=• )", classic):
        if not chunk.startswith("• "):
            continue
        quiz_id = re.search(r"Quiz ID: (\d+)", chunk)
        if not quiz_id:
            continue
        due = re.search(r"Due: (" + ISO.pattern + ")", chunk)
        submission = re.search(r"Your submission: (.*)", chunk)
        state = submission.group(1).strip() if submission else ""
        out.append({"id": quiz_id.group(1), "name": _unfence(chunk.splitlines()[0][2:])[:200],
                    "due_at": due.group(1) if due else None,
                    "submitted": state.startswith("submitted") or state == "excused"})
    return out


def parse_submissions(text):
    """`get_my_submission_status(course)` → {normalized name: state}. Canvas lists what's missing or unsubmitted in
    full (submitted work is capped at ten), so everything named in those sections is open, excused or graded ones are
    done, external tools are unknown, and an assignment named nowhere was submitted."""
    if not any(h in text for h in ("Missing Submissions", "No Canvas Submission Recorded", "External-tool assignments",
                                   "Submitted (")):
        return None  # not a format we know: report nothing rather than call everything submitted
    states, section = {}, None
    for line in text.splitlines():
        if "Missing Submissions" in line:
            section = "open"
        elif "No Canvas Submission Recorded" in line:
            section = "none"
        elif "External-tool assignments" in line:
            section = "unknown"
        elif "Submitted (" in line:
            section = "listed"
        elif line.startswith("• ") and section:
            current = _norm(_unfence(line[2:]))
            if section == "listed":
                states.setdefault(current, "submitted")
            else:
                states[current] = "unknown" if section == "unknown" else "open"
        elif line.strip().startswith("Status: ") and section == "none" and states:
            status = line.split("Status: ", 1)[1].strip()
            last = next(reversed(states))
            states[last] = "submitted" if status in ("EXCUSED", "GRADED") else "open"
    return states


def parse_announcements(text):
    """`list_my_announcements` output → [{"id", "title", "author", "posted_at", "url", "body"}]."""
    out = []
    for chunk in re.split(r"\n(?=• )", text):
        if not chunk.startswith("• "):
            continue
        ident = re.search(r"^\s*ID: (\d+)", chunk, re.M)
        posted = re.search(r"Posted (" + ISO.pattern + ")", chunk)
        if not ident or not posted:
            continue
        title = re.search(r"^\s*Title: (.*)$", chunk, re.M)
        author = re.search(r"^\s*Author: (.*)$", chunk, re.M)
        link = re.search(r"Link: (https://\S+)", chunk)
        body = BLOCK.search(chunk)
        out.append({"id": ident.group(1), "title": _unfence(title.group(1)) if title else "Announcement",
                    "unread": "[UNREAD]" in chunk.splitlines()[0],
                    "author": _unfence(author.group(1)) if author else "", "posted_at": posted.group(1),
                    "url": link.group(1) if link and link.group(1).startswith(_site()[0]) else "",
                    "body": body.group(1).strip() if body else ""})
    return out


def local_day(iso):
    """A Canvas timestamp → the Toronto calendar day it falls on (an 11:59 pm deadline stays on its day)."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_site()[1]).date().isoformat()


def local_stamp(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_site()[1]).isoformat(timespec="seconds")


def _key(title):
    """("a", 3) for "A3", "A 3", "Assignment 3", "Asst #3"; None when a title has no short form."""
    text = title.strip().lower()
    for word, short in WORDS.items():
        if re.match(re.escape(word) + r"\s*#?\s*\d", text):
            text = short + text[len(word):]
            break
    match = SHORT.match(text)
    return (match.group(1), int(match.group(2))) if match else None


def _adopt(candidates, name, day):
    """A task you made by hand for this item ("A3", due the same week), so the sync doesn't add a twin."""
    for task in candidates:
        same = _norm(task["title"]) == _norm(name) or (_key(task["title"]) and _key(task["title"]) == _key(name))
        close = task["due"] and abs((date.fromisoformat(task["due"]) - date.fromisoformat(day)).days) <= 3
        if same and (close or _norm(task["title"]) == _norm(name)):
            return task
    return None


def items(course_id, data):
    """Everything in one Canvas course that can be a task, with its state: assignments (graded quizzes included) and
    Classic quizzes that have no assignment."""
    states = data.get("submissions")
    names = [_norm(a["name"]) for a in data.get("assignments", [])]
    out = []
    for a in data.get("assignments", []):
        state = "unknown" if states is None or names.count(_norm(a["name"])) > 1 else states.get(_norm(a["name"]), "submitted")
        out.append({"external": f"canvas:{course_id}:{a['id']}", "name": a["name"], "due_at": a["due_at"], "state": state,
                    "link": f"{_site()[0]}/courses/{course_id}/assignments/{a['id']}"})
    for q in data.get("quizzes", []):
        if _norm(q["name"]) in names:
            continue
        out.append({"external": f"canvas:{course_id}:quiz:{q['id']}", "name": q["name"], "due_at": q["due_at"],
                    "state": "submitted" if q["submitted"] else "open",
                    "link": f"{_site()[0]}/courses/{course_id}/quizzes/{q['id']}"})
    return out


def pick_courses(store, canvas_courses):
    """Canvas course for each active Luna course: matched by code; if a code repeats (a retake), the newest wins."""
    luna = {c["code"]: c for c in store.list("courses", "active = 1")}
    chosen = {}
    for course in canvas_courses:
        owner = luna.get(course["code"])
        if owner and (course["code"] not in chosen or int(course["id"]) > int(chosen[course["code"]][0]["id"])):
            chosen[course["code"]] = (course, owner)
    return list(chosen.values())


def apply(store, canvas_courses, data, today=None):
    """Merge one sync's reads into tasks and announcements. `data` maps a Canvas course ID to {"assignments",
    "quizzes", "submissions", "announcements"} (a missing part was not read and is left alone). Returns counts."""
    today = today or date.today().isoformat()
    since = datetime.combine(date.fromisoformat(today) - timedelta(days=ANNOUNCEMENT_DAYS), datetime.min.time(), _site()[1]).isoformat()
    counts = dict.fromkeys(("created", "updated", "completed", "adopted", "announcements", "removed"), 0)
    for course, owner in pick_courses(store, canvas_courses):
        part = data.get(course["id"])
        if not part:
            continue
        for item in items(course["id"], part):
            _merge_task(store, owner, item, today, counts)
        if part.get("announcements") is not None:
            keep = set()
            for a in part["announcements"]:
                external = f"canvas:{course['id']}:announcement:{a['id']}"
                keep.add(external)
                if not store.one("SELECT 1 FROM announcements WHERE external_id = ?", (external,)):
                    counts["announcements"] += 1
                store.put_announcement(owner["id"], external, a["title"] or "Announcement", a["author"], a["body"], a["url"],
                                       local_stamp(a["posted_at"]), read=not a.get("unread", True))
            counts["removed"] += store.prune_announcements(owner["id"], f"canvas:{course['id']}:announcement:", keep, since)
    return counts


def _merge_task(store, owner, item, today, counts):
    if not item["due_at"]:
        return
    day, state = local_day(item["due_at"]), item["state"]
    task = store.one("SELECT * FROM tasks WHERE external_id = ?", (item["external"],))
    if task is None:
        candidates = store.list("tasks", "course_id = ? AND external_id IS NULL", (owner["id"],))
        task = _adopt(candidates, item["name"], day)
        if task is not None:
            store.db_set("tasks", task["id"], external_id=item["external"])
            counts["adopted"] += 1
        elif day < today or state in DONE_STATES:
            return  # history, or already handed in: not work to track
        else:
            task = store.create("tasks", {"title": item["name"] or "Assignment", "course_id": owner["id"], "due": day,
                                          "description": f"[Open in Quercus]({item['link']})\n"},
                                {"external_id": item["external"], "external_state": state})
            counts["created"] += 1
            return
    if task["due"] != day and task["status"] != "done":
        store.update("tasks", task["id"], {"due": day})
        counts["updated"] += 1
    if state != task.get("external_state"):
        if state in DONE_STATES and task["status"] != "done":
            store.set_task_status(task["id"], "done")
            counts["completed"] += 1
        store.db_set("tasks", task["id"], external_state=state)


async def fetch(runtime, store, today=None):
    """Read everything the sync needs for the active Luna courses. Returns (canvas courses, data per course) or raises
    SyncError with a reason code. A part that fails on its own (e.g. a course with its Quizzes page hidden) is left
    out, so it can't wipe what's stored."""
    async def call(name, arguments):
        result = await runtime.call_tool(INTEGRATION, name, arguments, bounded=False)
        if result["status"] != "completed":
            raise SyncError(result.get("reason") or result["status"])
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if text.startswith("Error"):
            raise SyncError("canvas_error")
        return text

    async def optional(name, arguments, parse):
        try:
            return parse(await call(name, arguments))
        except SyncError as error:
            if str(error) not in ("canvas_error", "unknown_tool"):
                raise
            return None

    courses = parse_courses(await call("list_courses", {}))
    today = today or date.today().isoformat()
    start = (date.fromisoformat(today) - timedelta(days=ANNOUNCEMENT_DAYS)).isoformat()
    data = {}
    for course, _owner in await asyncio.to_thread(pick_courses, store, courses):
        cid = course["id"]
        data[cid] = {"assignments": parse_assignments(await call("list_assignments", {"course_identifier": cid, "raw_dates": True})),
                     "quizzes": await optional("list_quizzes", {"course_identifier": cid}, parse_quizzes) or [],
                     "submissions": await optional("get_my_submission_status", {"course_identifier": cid}, parse_submissions),
                     "announcements": await optional("list_my_announcements", {
                         "course_identifier": cid, "start_date": start, "limit": 200, "preview_chars": 2000}, parse_announcements)}
        logger.info("Quercus {}: {} assignments, {} quizzes, {} announcements", course["code"], len(data[cid]["assignments"]),
                    len(data[cid]["quizzes"]), len(data[cid]["announcements"] or []))
    return courses, data


class Quercus:
    """The dashboard's keeper: one full read through a fresh canvas-mcp process, then a local merge. The token stays in
    Keychain and the server's environment; when the integration isn't set up, `sync` says so and does nothing."""

    def __init__(self, store, runtime_factory=None):
        self.store = store
        self.runtime_factory = runtime_factory or self._runtime
        self.lock = asyncio.Lock()

    @staticmethod
    def _runtime():
        from src.integrations.mcp import MCPRuntime
        configured = MCPRuntime.from_config()
        if INTEGRATION not in configured.integrations:
            return None
        return MCPRuntime([configured.integrations[INTEGRATION]])

    async def sync(self):
        """{"ok", "at", counts…} or {"ok": False, "reason"}; the result is also kept in the `quercus_sync` setting."""
        async with self.lock:
            runtime = self.runtime_factory()
            if runtime is None:
                return {"ok": False, "reason": "not_set_up"}
            try:
                courses, data = await fetch(runtime, self.store)
                status = {"ok": True, "at": time.time(), **await asyncio.to_thread(apply, self.store, courses, data)}
            except SyncError as error:
                status = {"ok": False, "at": time.time(), "reason": str(error)}
            finally:
                await runtime.close()
            await asyncio.to_thread(self.store.set_setting, "quercus_sync", status)
            if not status["ok"]:
                logger.warning("Quercus sync failed ({})", status["reason"])
            return status
