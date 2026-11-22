"""Private SQLite store for everything the dashboard tracks: projects, courses, tasks, notes, events, habits, money, people."""

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import uuid

from src.paths import HOME


DASHBOARD_PATH = HOME / "dashboard.sqlite3"
VERSION = 9

CATEGORIES = ("music", "software", "writing", "photography", "3d", "other")
TASK_STATUSES = ("todo", "doing", "done")
RSVPS = ("", "pending", "accepted", "maybe", "declined")  # "" for your own events; the rest answer an invitation
# Identity hues (docs/dashboard.md → Design language). A project's comes from its category; a course picks its own.
HUES = ("iris", "orchid", "rose", "peach", "sand", "sage", "lagoon", "sky")
CATEGORY_HUES = {"music": "rose", "software": "lagoon", "writing": "sand", "photography": "peach", "3d": "orchid", "other": "sky"}
COURSE_ICONS = ("book", "graduationcap", "function", "sum", "chart.bar", "cpu", "terminal", "atom", "flask", "leaf",
                "brain.head.profile", "globe.americas", "building.columns", "text.book.closed", "music.note", "paintbrush",
                "scalemass", "chart.line.uptrend.xyaxis", "bubble.left.and.text.bubble.right", "heart")
CATEGORY_ICONS = {"music": "music.note", "software": "chevron.left.forwardslash.chevron.right", "writing": "text.book.closed",
                  "photography": "camera", "3d": "cube", "other": "folder"}
# Every icon a project or course can wear: the subjects above, the categories, and a few more for projects.
ICONS = tuple(dict.fromkeys((*COURSE_ICONS, *CATEGORY_ICONS.values(), "waveform", "film", "house", "paintbrush.pointed",
                             "gamecontroller", "hammer", "sparkles", "globe", "cart", "airplane")))
# A new course's icon, from the letters of its code: CSC311 → cpu. Instant and local; anything unknown is a book.
SUBJECT_ICONS = {"CSC": "cpu", "CS": "cpu", "COMP": "cpu", "ECE": "cpu", "MAT": "function", "MATH": "function", "APM": "function",
                 "STA": "chart.bar", "STAT": "chart.bar", "PHY": "atom", "PHYS": "atom", "AST": "atom", "CHM": "flask",
                 "CHEM": "flask", "BIO": "leaf", "EEB": "leaf", "PSY": "brain.head.profile", "ECO": "chart.line.uptrend.xyaxis",
                 "ECON": "chart.line.uptrend.xyaxis", "HIS": "building.columns", "HIST": "building.columns", "POL": "building.columns",
                 "ENG": "text.book.closed", "LIT": "text.book.closed", "PHL": "bubble.left.and.text.bubble.right",
                 "PHIL": "bubble.left.and.text.bubble.right", "MUS": "music.note", "FAH": "paintbrush", "ART": "paintbrush",
                 "GGR": "globe.americas", "GEO": "globe.americas", "LIN": "bubble.left.and.text.bubble.right"}
COURSE_CODE = re.compile(r"^[A-Z]{2,4}\s?\d{2,4}[A-Z0-9]*$")
WORKOUT_KINDS = ("strength", "run", "cycle", "swim", "walk", "hike", "yoga", "hiit", "climb", "sport", "other")
METRIC_KINDS = ("steps", "active_energy", "exercise_minutes", "stand_hours", "resting_hr", "hrv", "sleep_minutes",
                "weight_kg", "body_fat", "vo2max", "water_ml", "protein_g", "calories")
ACCOUNT_KINDS = ("checking", "savings", "credit", "investment", "cash", "loan")
RELATIONS = ("family", "friend", "partner", "work", "music", "community", "other")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'other',
    category_label TEXT NOT NULL DEFAULT '', folder TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0, color TEXT NOT NULL DEFAULT '', icon TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY, code TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', folder TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT 'iris', icon TEXT NOT NULL DEFAULT 'book', active INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL, course_id TEXT REFERENCES courses(id) ON DELETE SET NULL,
    priority INTEGER NOT NULL DEFAULT 0, due TEXT, status TEXT NOT NULL DEFAULT 'todo', repeat TEXT NOT NULL DEFAULT '',
    done_at REAL, external_id TEXT, external_state TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    course_id TEXT REFERENCES courses(id) ON DELETE CASCADE, path TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS announcements (
    id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE, external_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS announcements_course ON announcements(course_id, posted_at);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, all_day INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', important INTEGER NOT NULL DEFAULT 0,
    course_id TEXT REFERENCES courses(id) ON DELETE SET NULL, organizer TEXT NOT NULL DEFAULT 'null',
    attendees TEXT NOT NULL DEFAULT '[]', rsvp TEXT NOT NULL DEFAULT '', uid TEXT UNIQUE, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_start ON events(start_at);
CREATE TABLE IF NOT EXISTS habits (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, symbol TEXT NOT NULL DEFAULT 'circle', target INTEGER NOT NULL DEFAULT 7,
    archived INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS habit_logs (
    habit_id TEXT NOT NULL REFERENCES habits(id) ON DELETE CASCADE, day TEXT NOT NULL, PRIMARY KEY (habit_id, day)
);
CREATE TABLE IF NOT EXISTS journal (
    day TEXT PRIMARY KEY, mood INTEGER, energy INTEGER, text TEXT NOT NULL DEFAULT '', gratitude TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workouts (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', started TEXT NOT NULL,
    duration_s INTEGER NOT NULL DEFAULT 0, distance_m REAL, energy_kcal REAL, avg_hr REAL, max_hr REAL,
    effort INTEGER, notes TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual', external_id TEXT UNIQUE,
    created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workouts_started ON workouts(started);
CREATE TABLE IF NOT EXISTS workout_sets (
    id TEXT PRIMARY KEY, workout_id TEXT NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
    exercise TEXT NOT NULL, reps INTEGER, weight_kg REAL, seq INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    kind TEXT NOT NULL, day TEXT NOT NULL, value REAL NOT NULL, source TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (kind, day, source)
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'checking', currency TEXT NOT NULL DEFAULT 'USD',
    balance REAL NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY, account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE, day TEXT NOT NULL,
    amount REAL NOT NULL, payee TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'Uncategorized',
    note TEXT NOT NULL DEFAULT '', external_id TEXT UNIQUE, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS transactions_day ON transactions(day);
CREATE TABLE IF NOT EXISTS budgets (category TEXT PRIMARY KEY, monthly REAL NOT NULL);
CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, relation TEXT NOT NULL DEFAULT 'friend', birthday TEXT,
    cadence_days INTEGER, notes TEXT NOT NULL DEFAULT '', handles TEXT NOT NULL DEFAULT '[]', last_contact TEXT,
    created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Writable columns per table and the value check for each. Anything else in a payload is rejected.
_text = str
_flag = lambda v: int(bool(v))  # noqa: E731


def _opt(kind):
    return lambda v: None if v in (None, "") else kind(v)


def _choice(options):
    def check(v):
        if v not in options:
            raise ValueError(f"Choose one of: {', '.join(options)}.")
        return v
    return check


def _day(v):
    if v in (None, ""):
        return None
    return date.fromisoformat(str(v)[:10]).isoformat()


def _stamp(v):
    return datetime.fromisoformat(str(v)).isoformat(timespec="minutes")


def _short(limit):
    return lambda v: " ".join(str(v or "").split())[:limit]


def _attendees(v):
    """Guests: a name and/or an email each, with their answer. Anything else in an entry is dropped."""
    out = []
    for a in v or []:
        name, email = " ".join(str(a.get("name") or "").split())[:120], str(a.get("email") or "").strip()[:200]
        if name or email:
            out.append({"name": name, "email": email, "rsvp": _choice(RSVPS[1:])(a.get("rsvp") or "pending"),
                        "organizer": bool(a.get("organizer"))})
    return json.dumps(out[:200])


def _organizer(v):
    if not v:
        return "null"
    name, email = " ".join(str(v.get("name") or "").split())[:120], str(v.get("email") or "").strip()[:200]
    return json.dumps({"name": name, "email": email}) if name or email else "null"


REPEATS = {"daily": 1, "weekly": 7, "biweekly": 14, "monthly": None}


def _handles(v):
    return json.dumps([{"network": str(h.get("network", "")), "value": str(h.get("value", ""))}
                       for h in (v or []) if h.get("value")])


def _repeat(v):
    return _choice(("", *REPEATS))(v or "")


def next_due(due, repeat):
    """The next occurrence's due date: an interval after the previous due date, even an overdue one (as in Notion).
    Monthly keeps the day of the month, clamped (Jan 31 → Feb 28). Without a due date, it counts from today."""
    start = date.fromisoformat(due) if due else today()
    if repeat == "monthly":
        first = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        last = (date(first.year + (first.month == 12), first.month % 12 + 1, 1) - timedelta(days=1)).day
        return first.replace(day=min(start.day, last)).isoformat()
    return (start + timedelta(days=REPEATS[repeat])).isoformat()


def _range(low, high, kind=int):
    def check(v):
        if v in (None, ""):
            return None
        v = kind(v)
        if not low <= v <= high:
            raise ValueError(f"Use a value from {low} to {high}.")
        return v
    return check


TABLES = {
    "projects": ({"title": _text, "description": _text, "category": _choice(CATEGORIES), "category_label": _short(40),
                  "folder": _text, "pinned": _flag, "archived": _flag, "color": _choice(("", *HUES)),
                  "icon": _choice(("", *ICONS))}, "archived, pinned DESC, updated DESC"),
    "courses": ({"code": _short(24), "description": _text, "folder": _text, "color": _choice(HUES),
                 "icon": _choice(ICONS), "active": _flag}, "active DESC, code COLLATE NOCASE"),
    "tasks": ({"title": _text, "description": _text, "project_id": _opt(str), "course_id": _opt(str),
               "priority": _range(0, 3), "due": _day, "status": _choice(TASK_STATUSES), "repeat": _repeat},
              "done_at IS NOT NULL, due IS NULL, due, priority DESC, created"),
    "notes": ({"title": _short(200), "project_id": _opt(str), "course_id": _opt(str)}, "created DESC"),
    # Announcements arrive from Quercus (src/dashboard/canvas.py); the only thing you change is whether you've read one.
    "announcements": ({"read": _flag}, "posted_at DESC"),
    "events": ({"title": _text, "start_at": _stamp, "end_at": _stamp, "all_day": _flag, "description": _text,
                "location": _text, "important": _flag, "course_id": _opt(str), "organizer": _organizer,
                "attendees": _attendees, "rsvp": _choice(RSVPS)}, "start_at"),
    "habits": ({"name": _text, "symbol": _text, "target": _range(1, 7), "archived": _flag}, "created"),
    "workouts": ({"kind": _choice(WORKOUT_KINDS), "title": _text, "started": _stamp, "duration_s": _opt(int),
                  "distance_m": _opt(float), "energy_kcal": _opt(float), "avg_hr": _opt(float), "max_hr": _opt(float),
                  "effort": _range(1, 10), "notes": _text, "source": _text, "external_id": _opt(str)}, "started DESC"),
    "workout_sets": ({"workout_id": _text, "exercise": _text, "reps": _opt(int), "weight_kg": _opt(float),
                      "seq": int}, "seq"),
    "accounts": ({"name": _text, "kind": _choice(ACCOUNT_KINDS), "currency": _text, "balance": float}, "kind, name"),
    "transactions": ({"account_id": _opt(str), "day": _day, "amount": float, "payee": _text, "category": _text,
                      "note": _text, "external_id": _opt(str)}, "day DESC, created DESC"),
    "people": ({"name": _text, "relation": _choice(RELATIONS), "birthday": _text, "cadence_days": _range(1, 730),
                "notes": _text, "handles": _handles, "last_contact": _day}, "name COLLATE NOCASE"),
}
JSON_COLUMNS = {"handles", "attendees", "organizer"}
REQUIRED = {"projects": ("title",), "courses": ("code",), "tasks": ("title",), "notes": ("title",), "announcements": ("title",),
            "events": ("title", "start_at", "end_at"), "habits": ("name",),
            "workouts": ("kind", "started"), "workout_sets": ("workout_id", "exercise"), "accounts": ("name",),
            "transactions": ("day", "amount"), "people": ("name",)}


def today():
    return date.today()


def project_look(row):
    """A project's hue and icon: its own when chosen, otherwise its category's."""
    return row.get("color") or CATEGORY_HUES[row["category"]], row.get("icon") or CATEGORY_ICONS[row["category"]]


def course_defaults(code, used=()):
    """A new course's colour and icon, decided locally in microseconds. The colour is the hue fewest active courses
    use, so courses taken together look different; ties break on the code, so the same code always lands the same way.
    The icon follows the subject letters (CSC → cpu, MAT → function), falling back to a book."""
    counts = {hue: list(used).count(hue) for hue in HUES}
    fewest = min(counts.values())
    free = [hue for hue in HUES if counts[hue] == fewest]
    seed = sum(ord(c) for c in (code or "").upper())
    letters = re.match(r"[A-Za-z]*", (code or "").strip()).group(0).upper()
    icon = SUBJECT_ICONS.get(letters) or SUBJECT_ICONS.get(letters[:3]) or "book"
    return free[seed % len(free)], icon


def _row(row):
    if row is None:
        return None
    out = dict(row)
    for key in JSON_COLUMNS & out.keys():
        if isinstance(out[key], str):  # an aggregate may reuse a JSON column's name ("items" as a count)
            out[key] = json.loads(out[key])
    return out


VIEWS = ("day", "week", "month")


def view_range(day, view):
    """Days a Home calendar view shows: the day, its week (Monday first), or every week touching its month."""
    if view == "day":
        return day, day
    if view == "week":
        first = day - timedelta(days=day.weekday())
        return first, first + timedelta(days=6)
    start = day.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return start - timedelta(days=start.weekday()), end + timedelta(days=6 - end.weekday())


def _month_end(d):
    return (d.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def period(day, view):
    """The span a view shows: the day itself, its week (Monday first), or its month."""
    if view == "week":
        first = day - timedelta(days=day.weekday())
        return first, first + timedelta(days=6)
    if view == "month":
        return day.replace(day=1), _month_end(day)
    return day, day


def _overlaps(event, first, last):
    """Whether an event falls on any day first..last. An end at midnight belongs to the day before."""
    begins, ends = datetime.fromisoformat(event["start"]), datetime.fromisoformat(event["end"])
    end_day = ends.date()
    if ends > begins and (event["all_day"] or ends.time() == datetime.min.time()):
        end_day -= timedelta(days=1)
    return begins.date() <= last and max(end_day, begins.date()) >= first


def _covers(event, d):
    return _overlaps(event, d, d)


def _extras(e):
    """What a native event adds on the calendar: where it came from, its hue and whether it's important."""
    return {"source": e.get("source", "calendar"), "important": bool(e.get("important")), "hue": e.get("hue"),
            "external_id": e.get("external_id", "")}


def _timed(d, events):
    """Timed events on day `d`, clipped to it. Recurring events share an id, so `key` adds the start."""
    out = []
    for e in events:
        if e["all_day"] or not _covers(e, d):
            continue
        start, end, midnight = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"]), datetime.combine(d, datetime.min.time())
        begins, ends = max(start, midnight), min(end, midnight + timedelta(hours=23, minutes=59))
        out.append({"kind": "event", "id": e["id"], "key": f"{e['id']}@{begins.isoformat(timespec='minutes')}", "title": e["title"],
                    "start": begins, "end": ends, "meta": e["location"], "calendar": e["calendar"], **_extras(e)})
    return sorted(out, key=lambda i: i["start"])


class Store:
    """One connection guarded by a lock; the web server calls it from worker threads."""

    def __init__(self, path=DASHBOARD_PATH, files=None):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        # Where notes and media live when their project or course has no folder (and standalone tasks' media).
        self.files = Path(files) if files else (path.parent if str(path) != ":memory:" else Path(tempfile.mkdtemp(prefix="luna-files-")))
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys = ON")
            if str(path) != ":memory:":
                self.db.execute("PRAGMA journal_mode = WAL")
                path.chmod(0o600)
            if self.db.execute("PRAGMA user_version").fetchone()[0] > VERSION:
                raise RuntimeError("This dashboard data needs a newer version of Luna.")
            with self.db:
                self.db.executescript(SCHEMA)
                self._migrate()
                self.db.execute(f"PRAGMA user_version = {VERSION}")
        except BaseException:
            self.db.close()
            raise

    def _migrate(self):
        """Bring older files up to date in place. v3–v5 grew tasks (estimate, no time, repeat); v6 is the productivity
        model: projects get a category and an archived flag instead of kind/status/progress/due, tasks get a status, a
        Markdown description and an optional course, and project items go (notes are Markdown files now). Areas that
        are course codes (CSC311) become courses and keep their tasks."""
        columns = lambda table: {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}  # noqa: E731
        old = columns("projects")
        if "kind" in old:
            self.db.execute("ALTER TABLE projects RENAME COLUMN summary TO description")
            for column, kind in (("category", "TEXT NOT NULL DEFAULT 'other'"), ("category_label", "TEXT NOT NULL DEFAULT ''"),
                                 ("archived", "INTEGER NOT NULL DEFAULT 0")):
                self.db.execute(f"ALTER TABLE projects ADD COLUMN {column} {kind}")
            self.db.execute("UPDATE projects SET category = CASE WHEN kind IN ('music', 'software', 'writing') THEN kind "
                            "ELSE 'other' END, category_label = CASE kind WHEN 'design' THEN 'Design' WHEN 'video' THEN 'Video' "
                            "WHEN 'research' THEN 'Research' ELSE '' END, archived = status IN ('archived', 'shipped')")
            for column in ("kind", "status", "url", "tags", "progress", "due"):
                self.db.execute(f"ALTER TABLE projects DROP COLUMN {column}")
        if "color" not in columns("projects"):  # v7: a project can wear its own colour and icon ("" = its category's)
            self.db.execute("ALTER TABLE projects ADD COLUMN color TEXT NOT NULL DEFAULT ''")
            self.db.execute("ALTER TABLE projects ADD COLUMN icon TEXT NOT NULL DEFAULT ''")
        old = columns("tasks")
        if "notes" in old:
            self.db.execute("ALTER TABLE tasks RENAME COLUMN notes TO description")
        if "status" not in old:
            self.db.execute("ALTER TABLE tasks ADD COLUMN status TEXT NOT NULL DEFAULT 'todo'")
            self.db.execute("UPDATE tasks SET status = 'done' WHERE done_at IS NOT NULL")
        if "repeat" not in old:
            self.db.execute("ALTER TABLE tasks ADD COLUMN repeat TEXT NOT NULL DEFAULT ''")
        if "course_id" not in old:
            self.db.execute("ALTER TABLE tasks ADD COLUMN course_id TEXT REFERENCES courses(id) ON DELETE SET NULL")
        if "area" in old:
            codes = sorted({r[0].strip() for r in self.db.execute("SELECT area FROM tasks WHERE area != ''")
                            if COURSE_CODE.match(r[0].strip().upper())})
            for code in codes:
                used = [r[0] for r in self.db.execute("SELECT color FROM courses WHERE active = 1")]
                color, icon = course_defaults(code, used)
                course_id, now = uuid.uuid4().hex, time.time()
                self.db.execute("INSERT INTO courses (id, code, color, icon, created, updated) VALUES (?, ?, ?, ?, ?, ?)",
                                (course_id, code.upper(), color, icon, now, now))
                self.db.execute("UPDATE tasks SET course_id = ? WHERE trim(area) = ? AND project_id IS NULL", (course_id, code))
        for column in ("area", "minutes", "time"):
            if column in old:
                self.db.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
        self.db.execute("DROP TABLE IF EXISTS project_items")
        if "external_id" not in columns("tasks"):  # v8: where a synced task came from (canvas:<course>:<assignment>)
            self.db.execute("ALTER TABLE tasks ADD COLUMN external_id TEXT")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS tasks_external ON tasks(external_id)")
        if "external_state" not in columns("tasks"):  # v9: the source's last state (Quercus: open/submitted/unknown)
            self.db.execute("ALTER TABLE tasks ADD COLUMN external_state TEXT NOT NULL DEFAULT ''")

    def close(self):
        with self.lock:
            self.db.close()

    def query(self, sql, args=()):
        with self.lock:
            return [_row(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.lock:
            return _row(self.db.execute(sql, args).fetchone())

    # Generic records -------------------------------------------------------------------------------------------

    def _clean(self, table, data, *, creating):
        if table not in TABLES:
            raise KeyError(table)
        columns, _ = TABLES[table]
        unknown = set(data) - set(columns) - {"id"}
        if unknown:
            raise ValueError(f"Unknown field: {sorted(unknown)[0]}.")
        clean = {key: columns[key](value) for key, value in data.items() if key != "id"}
        if creating:
            missing = [key for key in REQUIRED[table] if clean.get(key) in (None, "")]
            if missing:
                raise ValueError(f"{missing[0].replace('_', ' ').capitalize()} is required.")
        for key in REQUIRED[table]:
            if key in clean and not str(clean[key]).strip():
                raise ValueError(f"{key.capitalize()} can't be empty.")
        return clean

    def list(self, table, where=None, args=(), limit=None):
        order = TABLES[table][1]
        sql = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "") + f" ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql, args)

    def get(self, table, record_id):
        if table not in TABLES:
            raise KeyError(table)
        return self.one(f"SELECT * FROM {table} WHERE id = ?", (record_id,))

    def _check(self, table, clean, existing=None):
        """Rules that span fields: one owner per task or note, a task's done time follows its status, an event can't end
        before it starts, and a new course gets its colour and icon when none was chosen."""
        merged = dict(existing or {}) | clean
        if table in ("tasks", "notes") and merged.get("project_id") and merged.get("course_id"):
            raise ValueError("Choose a project or a course, not both.")
        if table == "tasks" and "status" in clean:
            was = existing["status"] if existing else None
            clean["done_at"] = (existing["done_at"] or time.time()) if clean["status"] == "done" and was == "done" else (
                time.time() if clean["status"] == "done" else None)
        if table == "events" and merged.get("start_at") and merged.get("end_at") and merged["end_at"] < merged["start_at"]:
            raise ValueError("An event can't end before it starts.")
        if table == "courses" and existing is None:
            color, icon = course_defaults(clean.get("code"), [r["color"] for r in self.query("SELECT color FROM courses WHERE active = 1")])
            clean.setdefault("color", color)
            clean.setdefault("icon", icon)
        return clean

    def create(self, table, data, internal=None):
        """`internal` carries columns the API can't write (a note's file path)."""
        clean = self._check(table, self._clean(table, data, creating=True)) | (internal or {})
        now = time.time()
        clean |= {"id": data.get("id") or uuid.uuid4().hex, "created": now, "updated": now}
        with self.lock, self.db:
            self.db.execute(f"INSERT INTO {table} ({', '.join(clean)}) VALUES ({', '.join('?' * len(clean))})",
                            tuple(clean.values()))
            self._touch_parent(table, clean)
        return self.get(table, clean["id"])

    def update(self, table, record_id, data):
        clean = self._clean(table, data, creating=False)
        if table in ("tasks", "notes", "events"):
            existing = self.get(table, record_id)
            if existing is None:
                raise LookupError("That item no longer exists.")
            clean = self._check(table, clean, existing)
        clean["updated"] = time.time()
        with self.lock, self.db:
            changed = self.db.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in clean)} WHERE id = ?",
                                      (*clean.values(), record_id)).rowcount
            if not changed:
                raise LookupError("That item no longer exists.")
            self._touch_parent(table, self.db.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone())
        return self.get(table, record_id)

    def db_set(self, table, record_id, **values):
        """Set columns the API can't write, such as a note's path after a rename."""
        with self.lock, self.db:
            self.db.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", (*values.values(), record_id))

    def delete(self, table, record_id):
        if table not in TABLES:
            raise KeyError(table)
        with self.lock, self.db:
            if table in ("projects", "courses"):  # its tasks go with it; its events stay on the calendar
                self.db.execute(f"DELETE FROM tasks WHERE {table[:-1]}_id = ?", (record_id,))
            row = self.db.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
            self.db.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
            if row is not None:
                self._touch_parent(table, row)

    def _touch_parent(self, table, row):
        """A project or course counts as touched when its tasks or notes change (Home orders projects by it)."""
        if table in ("tasks", "notes") and row is not None:
            row = dict(row)
            if row.get("project_id"):
                self.db.execute("UPDATE projects SET updated = ? WHERE id = ?", (time.time(), row["project_id"]))
            if row.get("course_id"):
                self.db.execute("UPDATE courses SET updated = ? WHERE id = ?", (time.time(), row["course_id"]))

    # Settings ---------------------------------------------------------------------------------------------------

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    # Projects and courses ---------------------------------------------------------------------------------------

    def projects(self):
        """Every project with its progress: done over all tasks, leaving out repeating ones (they never finish)."""
        rows = self.list("projects")
        stats = {r["project_id"]: r for r in self.query(
            "SELECT project_id, COALESCE(SUM(repeat = ''), 0) AS total, COALESCE(SUM(repeat = '' AND status = 'done'), 0) AS done, "
            "COALESCE(SUM(status != 'done'), 0) AS open, MAX(updated) AS last_task FROM tasks WHERE project_id IS NOT NULL "
            "GROUP BY project_id")}
        for row in rows:
            st = stats.get(row["id"], {})
            row["tasks_total"], row["tasks_done"] = st.get("total", 0), st.get("done", 0)
            row["progress"] = row["tasks_done"] / row["tasks_total"] if row["tasks_total"] else 0
            row["open_tasks"], row["last_task"] = st.get("open", 0), st.get("last_task")
            row["hue"], row["symbol"] = project_look(row)
        return rows

    def project(self, project_id):
        row = next((p for p in self.projects() if p["id"] == project_id), None)
        if row is None:
            return None
        row["tasks"] = self.tasks_for("project_id", project_id)
        row["notes"] = self.list("notes", "project_id = ?", (project_id,))
        return row

    def courses(self):
        rows = self.list("courses")
        open_tasks = {r["course_id"]: r["n"] for r in self.query(
            "SELECT course_id, COUNT(*) AS n FROM tasks WHERE status != 'done' AND course_id IS NOT NULL GROUP BY course_id")}
        notes = {r["course_id"]: r["n"] for r in self.query(
            "SELECT course_id, COUNT(*) AS n FROM notes WHERE course_id IS NOT NULL GROUP BY course_id")}
        unread = {r["course_id"]: r["n"] for r in self.query(
            "SELECT course_id, COUNT(*) AS n FROM announcements WHERE read = 0 GROUP BY course_id")}
        for row in rows:
            row["open_tasks"], row["notes_count"] = open_tasks.get(row["id"], 0), notes.get(row["id"], 0)
            row["unread_announcements"] = unread.get(row["id"], 0)
        return rows

    def course(self, course_id):
        row = next((c for c in self.courses() if c["id"] == course_id), None)
        if row is None:
            return None
        row["tasks"] = self.tasks_for("course_id", course_id)
        row["notes"] = self.list("notes", "course_id = ?", (course_id,))
        row["announcements"] = self.list("announcements", "course_id = ?", (course_id,), limit=50)
        now = datetime.now().isoformat(timespec="minutes")
        row["events"] = [self._event(e) for e in self.query(
            "SELECT * FROM events WHERE course_id = ? AND end_at >= ? ORDER BY start_at LIMIT 30", (course_id, now))]
        return row

    def owners(self):
        """What a task or note can belong to: open projects and active courses, for pickers."""
        return {"projects": [{"id": p["id"], "title": p["title"], "category": p["category"], "hue": project_look(p)[0]}
                             for p in self.list("projects", "archived = 0")],
                "courses": [{"id": c["id"], "code": c["code"], "color": c["color"], "icon": c["icon"]}
                            for c in self.list("courses", "active = 1")]}

    def put_announcement(self, course_id, external_id, title, author, body, url, posted_at, read=False):
        """Insert or refresh one synced announcement. A new one starts read if you'd read it on Quercus; after that its
        read state is yours (an edit doesn't change it)."""
        now = time.time()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO announcements (id, course_id, external_id, title, author, body, url, posted_at, read, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(external_id) DO UPDATE SET title = excluded.title, "
                "author = excluded.author, body = excluded.body, url = excluded.url, posted_at = excluded.posted_at, "
                "course_id = excluded.course_id, updated = excluded.updated WHERE title != excluded.title OR "
                "body != excluded.body OR posted_at != excluded.posted_at OR author != excluded.author",
                (uuid.uuid4().hex, course_id, external_id, title, author, body, url, posted_at, int(read), now, now))

    def prune_announcements(self, course_id, prefix, keep, since):
        """Drop announcements Quercus no longer has (deleted or withdrawn) within the window that was read."""
        rows = self.query("SELECT id, external_id FROM announcements WHERE course_id = ? AND external_id LIKE ? AND posted_at >= ?",
                          (course_id, prefix + "%", since))
        gone = [r["id"] for r in rows if r["external_id"] not in keep]
        with self.lock, self.db:
            self.db.executemany("DELETE FROM announcements WHERE id = ?", [(i,) for i in gone])
        return len(gone)

    # Tasks ------------------------------------------------------------------------------------------------------

    def tasks_for(self, column, owner_id):
        return self.list("tasks", f"{column} = ?", (owner_id,))

    def _owner_active(self, task):
        if task["project_id"]:
            return bool(self.one("SELECT 1 FROM projects WHERE id = ? AND archived = 0", (task["project_id"],)))
        if task["course_id"]:
            return bool(self.one("SELECT 1 FROM courses WHERE id = ? AND active = 1", (task["course_id"],)))
        return True

    def set_task_status(self, task_id, status):
        """Move a task between not started, in progress and done. Finishing a repeating task creates its next occurrence,
        once: only on the change into done, and only while its project or course is still active (archiving one ends
        its repeats). The result carries it as `next`. Leaving done removes that occurrence again while it's untouched,
        so done, undone, done leaves exactly one."""
        with self.lock:
            task = self.get("tasks", task_id)
            if task is None:
                raise LookupError("That item no longer exists.")
            row = self.update("tasks", task_id, {"status": status})
            if status == "done" and task["status"] != "done" and task["repeat"] and self._owner_active(task):
                fields = {k: task[k] for k in ("title", "description", "project_id", "course_id", "priority", "repeat")}
                row["next"] = self.create("tasks", fields | {"due": next_due(task["due"], task["repeat"])})
            elif status != "done" and task["status"] == "done" and task["repeat"]:
                spawned = self.one("SELECT id FROM tasks WHERE done_at IS NULL AND title = ? AND repeat = ? AND due = ? AND id != ? "
                                   "AND created >= ? AND updated = created ORDER BY created LIMIT 1",
                                   (task["title"], task["repeat"], next_due(task["due"], task["repeat"]), task_id, task["done_at"] - 1))
                if spawned:
                    self.delete("tasks", spawned["id"])
                    row["removed"] = spawned["id"]
        return row

    def complete_task(self, task_id, done=True):
        return self.set_task_status(task_id, "done" if done else "todo")

    def _with_owner(self, rows):
        """Tasks as Home lists them: each names what it belongs to (project title or course code) in that owner's hue."""
        projects = {p["id"]: p for p in self.query("SELECT id, title, category, color, icon FROM projects")}
        courses = {c["id"]: c for c in self.query("SELECT id, code, color FROM courses")}
        out = []
        for t in rows:
            owner = None
            if t["project_id"] in projects:
                p = projects[t["project_id"]]
                owner = {"kind": "project", "id": p["id"], "label": p["title"], "hue": project_look(p)[0]}
            elif t["course_id"] in courses:
                c = courses[t["course_id"]]
                owner = {"kind": "course", "id": c["id"], "label": c["code"], "hue": c["color"]}
            out.append({"id": t["id"], "title": t["title"], "due": t["due"], "priority": t["priority"], "repeat": t["repeat"],
                        "status": t["status"], "done_at": t["done_at"], "owner": owner, "meta": owner and owner["label"]})
        return out

    def recently_done(self, days=14, limit=30):
        """Tasks finished in the last two weeks, newest first, for Home's Completed list."""
        since = time.time() - days * 86400
        return self._with_owner(self.query("SELECT * FROM tasks WHERE done_at >= ? ORDER BY done_at DESC LIMIT ?", (since, limit)))

    def tasks(self, scope="open"):
        if scope == "done":
            return self.query("SELECT * FROM tasks WHERE done_at IS NOT NULL ORDER BY done_at DESC LIMIT 100")
        if scope == "all":
            return self.list("tasks")
        if scope == "standalone":
            return self.list("tasks", "project_id IS NULL AND course_id IS NULL")
        return self.list("tasks", "done_at IS NULL")

    # Events -----------------------------------------------------------------------------------------------------

    def _event(self, row, courses=None):
        """A native event in the shape calendar.py gives EventKit's, plus what only Luna has (importance, guests)."""
        course = (courses or {}).get(row["course_id"]) if courses is not None else (
            self.one("SELECT id, code, color FROM courses WHERE id = ?", (row["course_id"],)) if row["course_id"] else None)
        return {"id": row["id"], "title": row["title"], "start": row["start_at"], "end": row["end_at"], "all_day": bool(row["all_day"]),
                "description": row["description"], "location": row["location"], "important": bool(row["important"]),
                "course_id": row["course_id"], "course": course and course["code"], "hue": course and course["color"],
                "organizer": row["organizer"], "attendees": row["attendees"], "rsvp": row["rsvp"] or None,
                "calendar": course["code"] if course else "Luna", "source": "luna", "external_id": "",
                "created": row["created"], "updated": row["updated"]}

    def event(self, event_id):
        row = self.get("events", event_id)
        return row and self._event(row)

    def events_between(self, first, last):
        """Native events touching first..last (dates). All-day events end at midnight after their last day."""
        courses = {c["id"]: c for c in self.query("SELECT id, code, color FROM courses")}
        lo, hi = f"{first.isoformat()}T00:00", f"{(last + timedelta(days=1)).isoformat()}T00:00"
        return [self._event(r, courses) for r in self.query(
            "SELECT * FROM events WHERE start_at < ? AND end_at >= ? ORDER BY start_at", (hi, lo))]

    def invitations(self, days=30):
        """Invitations you haven't answered, or answered maybe, from now on."""
        now = datetime.now()
        return [e for e in self.events_between(now.date(), now.date() + timedelta(days=days))
                if e["rsvp"] in ("pending", "maybe") and datetime.fromisoformat(e["end"]) > now]

    # Habits -----------------------------------------------------------------------------------------------------

    def habits(self, days=84):
        start = today() - timedelta(days=days - 1)
        rows = self.list("habits", "archived = 0")
        logs = {}
        for log in self.query("SELECT * FROM habit_logs WHERE day >= ?", (start.isoformat(),)):
            logs.setdefault(log["habit_id"], []).append(log["day"])
        for row in rows:
            done = set(logs.get(row["id"], []))
            row["days"] = sorted(done)
            row["streak"] = _streak(done)
            week = {(today() - timedelta(days=i)).isoformat() for i in range(7)}
            row["week"] = len(done & week)
        return rows

    def toggle_habit(self, habit_id, day=None):
        day = _day(day) or today().isoformat()
        with self.lock, self.db:
            if self.db.execute("DELETE FROM habit_logs WHERE habit_id = ? AND day = ?", (habit_id, day)).rowcount:
                return False
            self.db.execute("INSERT INTO habit_logs VALUES (?, ?)", (habit_id, day))
            return True

    # Journal ----------------------------------------------------------------------------------------------------

    def journal(self, limit=60):
        return self.query("SELECT * FROM journal ORDER BY day DESC LIMIT ?", (limit,))

    def write_journal(self, day, data):
        day = _day(day) or today().isoformat()
        mood, energy = _range(1, 5)(data.get("mood")), _range(1, 5)(data.get("energy"))
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO journal (day, mood, energy, text, gratitude, updated) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET mood = excluded.mood, energy = excluded.energy, text = excluded.text, "
                "gratitude = excluded.gratitude, updated = excluded.updated",
                (day, mood, energy, str(data.get("text", "")), str(data.get("gratitude", "")), time.time()))
        return self.one("SELECT * FROM journal WHERE day = ?", (day,))

    # Fitness ----------------------------------------------------------------------------------------------------

    def set_metric(self, kind, day, value, source="manual"):
        _choice(METRIC_KINDS)(kind)
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO metrics VALUES (?, ?, ?, ?)",
                            (kind, _day(day) or today().isoformat(), float(value), source))

    def metric_series(self, kind, days=90):
        start = (today() - timedelta(days=days - 1)).isoformat()
        # A manual entry wins over imported data for the same day.
        return self.query(
            "SELECT day, COALESCE(MAX(CASE WHEN source = 'manual' THEN value END), MAX(value)) AS value "
            "FROM metrics WHERE kind = ? AND day >= ? GROUP BY day ORDER BY day", (kind, start))

    def workouts(self, days=None, limit=200):
        where, args = (("started >= ?", ((today() - timedelta(days=days)).isoformat(),)) if days else (None, ()))
        rows = self.list("workouts", where, args, limit)
        sets = {}
        if rows:
            marks = ",".join("?" * len(rows))
            for s in self.query(f"SELECT * FROM workout_sets WHERE workout_id IN ({marks}) ORDER BY seq",
                                [r["id"] for r in rows]):
                sets.setdefault(s["workout_id"], []).append(s)
        for row in rows:
            row["sets"] = sets.get(row["id"], [])
            row["volume_kg"] = sum((s["reps"] or 0) * (s["weight_kg"] or 0) for s in row["sets"])
        return rows

    def save_workout(self, data, workout_id=None):
        sets = data.pop("sets", None)
        with self.lock:
            row = self.update("workouts", workout_id, data) if workout_id else self.create("workouts", data)
            if sets is not None:
                with self.db:
                    self.db.execute("DELETE FROM workout_sets WHERE workout_id = ?", (row["id"],))
                for seq, item in enumerate(sets):
                    if str(item.get("exercise", "")).strip():
                        self.create("workout_sets", {"workout_id": row["id"], "exercise": item["exercise"].strip(),
                                                     "reps": item.get("reps"), "weight_kg": item.get("weight_kg"),
                                                     "seq": seq})
        return row

    def fitness(self):
        now = today()
        weeks = []
        for back in range(11, -1, -1):
            start = now - timedelta(days=now.weekday() + 7 * back)
            end = start + timedelta(days=7)
            stats = self.one(
                "SELECT COUNT(*) AS sessions, COALESCE(SUM(duration_s), 0) AS seconds, "
                "COALESCE(SUM(distance_m), 0) AS meters, COALESCE(SUM(energy_kcal), 0) AS kcal "
                "FROM workouts WHERE started >= ? AND started < ?", (start.isoformat(), end.isoformat()))
            weeks.append({"week": start.isoformat(), **stats})
        mix = self.query("SELECT kind, COUNT(*) AS n, SUM(duration_s) AS seconds FROM workouts WHERE started >= ? "
                         "GROUP BY kind ORDER BY seconds DESC", ((now - timedelta(days=90)).isoformat(),))
        records = self.query(
            "SELECT exercise, MAX(weight_kg) AS best_kg, MAX(reps * weight_kg) AS best_set FROM workout_sets "
            "WHERE weight_kg IS NOT NULL GROUP BY exercise ORDER BY COUNT(*) DESC LIMIT 8")
        days = {r["day"] for r in self.query("SELECT DISTINCT substr(started, 1, 10) AS day FROM workouts")}
        metrics = {kind: self.metric_series(kind, 120) for kind in METRIC_KINDS}
        return {"weeks": weeks, "mix": mix, "records": records, "streak": _streak(days, grace=1),
                "metrics": {k: v for k, v in metrics.items() if v}}

    # Money ------------------------------------------------------------------------------------------------------

    def set_budget(self, category, monthly):
        with self.lock, self.db:
            if monthly in (None, "", 0):
                self.db.execute("DELETE FROM budgets WHERE category = ?", (category,))
            else:
                self.db.execute("INSERT OR REPLACE INTO budgets VALUES (?, ?)", (category, float(monthly)))

    def money(self, month=None):
        month = month or today().strftime("%Y-%m")
        spend = self.query("SELECT category, -SUM(amount) AS spent, COUNT(*) AS n FROM transactions "
                           "WHERE substr(day, 1, 7) = ? AND amount < 0 GROUP BY category ORDER BY spent DESC", (month,))
        income = self.one("SELECT COALESCE(SUM(amount), 0) AS v FROM transactions WHERE substr(day, 1, 7) = ? "
                          "AND amount > 0", (month,))["v"]
        budgets = {r["category"]: r["monthly"] for r in self.query("SELECT * FROM budgets")}
        trend = self.query("SELECT substr(day, 1, 7) AS month, -SUM(CASE WHEN amount < 0 THEN amount END) AS spent, "
                           "SUM(CASE WHEN amount > 0 THEN amount END) AS income FROM transactions "
                           "GROUP BY 1 ORDER BY 1 DESC LIMIT 12")[::-1]
        accounts = self.list("accounts")
        return {"month": month, "accounts": accounts, "income": income, "spend": spend, "budgets": budgets,
                "trend": trend, "recurring": self.recurring(),
                "net_worth": sum(a["balance"] * (-1 if a["kind"] in ("credit", "loan") else 1) for a in accounts),
                "transactions": self.list("transactions", "substr(day, 1, 7) = ?", (month,), 300)}

    def recurring(self):
        """Payees charged in at least three distinct months with similar amounts: likely subscriptions."""
        rows = self.query("SELECT payee, category, COUNT(DISTINCT substr(day, 1, 7)) AS months, AVG(amount) AS amount, "
                          "MIN(amount) AS low, MAX(amount) AS high, MAX(day) AS last FROM transactions "
                          "WHERE amount < 0 AND payee != '' GROUP BY payee HAVING months >= 3")
        return [r for r in rows if abs(r["high"] - r["low"]) <= max(1.0, abs(r["amount"]) * 0.15)]

    # People -----------------------------------------------------------------------------------------------------

    def people(self):
        rows = self.list("people")
        for row in rows:
            row["due"] = None
            if row["cadence_days"]:
                last = date.fromisoformat(row["last_contact"]) if row["last_contact"] else None
                row["due"] = (last + timedelta(days=row["cadence_days"])).isoformat() if last else today().isoformat()
            row["next_birthday"] = _next_birthday(row["birthday"])
        return rows

    def touch_person(self, person_id, day=None):
        return self.update("people", person_id, {"last_contact": day or today().isoformat()})

    # Overview ---------------------------------------------------------------------------------------------------

    def overview(self):
        now = today()
        horizon = (now + timedelta(days=7)).isoformat()
        tasks = self.list("tasks", "done_at IS NULL AND (due IS NULL OR due <= ?)", (horizon,), 12)
        done_today = self.one("SELECT COUNT(*) AS n FROM tasks WHERE date(done_at, 'unixepoch', 'localtime') = ?",
                              (now.isoformat(),))["n"]
        people = [p for p in self.people() if (p["due"] and p["due"] <= now.isoformat()) or
                  (p["next_birthday"] and p["next_birthday"] <= (now + timedelta(days=14)).isoformat())]
        week_start = (now - timedelta(days=now.weekday())).isoformat()
        moves = self.one("SELECT COUNT(*) AS sessions, COALESCE(SUM(duration_s), 0) AS seconds FROM workouts "
                         "WHERE started >= ?", (week_start,))
        steps = self.metric_series("steps", 7)
        sleep = self.metric_series("sleep_minutes", 7)
        spent = self.one("SELECT COALESCE(-SUM(amount), 0) AS v FROM transactions WHERE amount < 0 AND "
                         "substr(day, 1, 7) = ?", (now.strftime("%Y-%m"),))["v"]
        budget = self.one("SELECT COALESCE(SUM(monthly), 0) AS v FROM budgets")["v"]
        return {
            "date": now.isoformat(), "tasks": tasks, "done_today": done_today, "habits": self.habits(14),
            "projects": [p for p in self.projects() if not p["archived"]][:6], "people": people[:6],
            "fitness": {**moves, "steps": steps, "sleep": sleep, "target_sessions": self.setting("weekly_sessions", 4)},
            "money": {"spent": spent, "budget": budget},
            "journal": self.one("SELECT * FROM journal WHERE day = ?", (now.isoformat(),)),
            "moods": self.query("SELECT day, mood, energy FROM journal WHERE day >= ? ORDER BY day",
                                ((now - timedelta(days=29)).isoformat(),)),
        }

    # Home -------------------------------------------------------------------------------------------------------

    def day_plan(self, day, events, view="day"):
        """Home for one day: its calendar (day, week or month view) and open tasks by due date.

        `events` are calendar events overlapping `view_range(day, view)` (see calendar.py)."""
        iso = day.isoformat()
        now = datetime.now()
        current = today()
        first, last = view_range(day, view)
        people = self.people()
        extras = self._all_day_extras(first, last, people)
        # The corona edge goes to the next or current event, and only today.
        upcoming = [i for i in _timed(current, events) if i["end"] > now]
        next_key = upcoming[0]["key"] if upcoming else None

        def calendar_day(d):
            timeline = []
            for item in _timed(d, events):
                item["past"], item["next"] = item["end"] <= now, item["key"] == next_key
                item["start"], item["end"] = item["start"].strftime("%H:%M"), item["end"].strftime("%H:%M")
                timeline.append(item)
            all_day = [{"kind": "event", "id": e["id"], "title": e["title"], "meta": e["calendar"], **_extras(e)}
                       for e in events if e["all_day"] and _covers(e, d)]
            return {"date": d.isoformat(), "timeline": timeline, "all_day": all_day + extras.get(d.isoformat(), [])}

        shown = calendar_day(day)
        days = [calendar_day(first + timedelta(days=n)) for n in range((last - first).days + 1)] if view != "day" else []

        # Tasks are defined by when they're due, never by when to do them: every open task with a due date, however far
        # out, overdue first (by date), then by priority. Undated ones are a backlog and stay off Home.
        tasks = [t | {"late": t["due"] < current.isoformat()} for t in self._with_owner(self.list("tasks", "done_at IS NULL AND due IS NOT NULL"))]
        tasks.sort(key=lambda t: (t["due"], -t["priority"]))

        # The week view's day row carries one dot per event, task due or birthday.
        if view == "week":
            due = {r["due"]: r["n"] for r in self.query("SELECT due, COUNT(*) AS n FROM tasks WHERE done_at IS NULL AND due BETWEEN ? AND ? "
                                                         "GROUP BY due", (first.isoformat(), last.isoformat()))}
            for x in days:
                d = date.fromisoformat(x["date"])
                x["count"] = (sum(1 for e in events if _covers(e, d)) + due.get(x["date"], 0)
                              + sum(1 for p in people if p["next_birthday"] == x["date"]))
        span = period(day, view)
        return {"date": iso, "today": day == current, "now": now.strftime("%H:%M") if day == current else None, "view": view,
                "period": [span[0].isoformat(), span[1].isoformat()], "has_today": span[0] <= current <= span[1],
                "timeline": shown["timeline"], "all_day": shown["all_day"], "days": days, "tasks": tasks}

    def glance(self, events):
        """Today at a glance for Home's greeting. `events` cover today and tomorrow."""
        now, current = datetime.now(), today()
        todays = _timed(current, events)
        tomorrow = _timed(current + timedelta(days=1), events)
        due = self.one("SELECT COUNT(*) AS n, COALESCE(SUM(due < ?), 0) AS late FROM tasks WHERE done_at IS NULL AND due <= ?",
                       (current.isoformat(), current.isoformat()))
        ahead = [i for i in todays if i["end"] > now]
        return {"events_today": len(todays), "events_left": len(ahead),
                "next": {"title": ahead[0]["title"], "start": ahead[0]["start"].strftime("%H:%M")} if ahead else None,
                "tasks_due": due["n"] - due["late"], "late": due["late"],
                "tomorrow": {"title": tomorrow[0]["title"], "start": tomorrow[0]["start"].strftime("%H:%M")} if tomorrow else None}

    def _all_day_extras(self, first, last, people):
        """Birthdays for the all-day row, by day."""
        out = {}
        lo, hi = first.isoformat(), last.isoformat()
        for p in people:
            if p["next_birthday"] and lo <= p["next_birthday"] <= hi:
                out.setdefault(p["next_birthday"], []).append({"kind": "birthday", "id": p["id"], "title": f"{p['name']}'s birthday",
                                                               "meta": "People"})
        return out

    def alerts(self):
        """What deserves a badge: overdue work and people's days."""
        now = today().isoformat()
        out = []
        for t in self.list("tasks", "done_at IS NULL AND due < ?", (now,)):
            out.append({"kind": "task", "title": t["title"], "meta": "Overdue", "link": task_link(t)})
        for p in self.people():
            if p["next_birthday"] == now:
                out.append({"kind": "birthday", "title": f"{p['name']}'s birthday", "meta": "Today", "link": "home"})
        return out

    def search(self, text, limit=24):
        like = f"%{text.strip()}%"
        if len(text.strip()) < 2:
            return []
        hits = []
        for table, label, columns, kind in (
            ("projects", "title", ("title", "description"), "project"), ("courses", "code", ("code", "description"), "course"),
            ("tasks", "title", ("title", "description"), "task"), ("notes", "title", ("title",), "note"),
            ("events", "title", ("title", "location", "description"), "event"), ("people", "name", ("name", "notes"), "person"),
            ("workouts", "title", ("title", "notes"), "workout"), ("transactions", "payee", ("payee", "note"), "transaction"),
        ):
            where = "(" + " OR ".join(f"{c} LIKE ?" for c in columns) + ")"
            for row in self.query(f"SELECT * FROM {table} WHERE {where} LIMIT 6", (like,) * len(columns)):
                hit = {"kind": kind, "id": row["id"], "title": row[label]}
                if table in ("tasks", "notes"):
                    hit["link"] = task_link(row) if table == "tasks" else note_link(row)
                if table == "events":
                    hit["link"] = f"home/event/{row['id']}"
                hits.append(hit)
        for row in self.query("SELECT day, text FROM journal WHERE text LIKE ? ORDER BY day DESC LIMIT 4", (like,)):
            hits.append({"kind": "journal", "id": row["day"], "title": row["day"]})
        return hits[:limit]


def task_link(task):
    """Where a task opens: inside its project or course, or on its own."""
    if task["project_id"]:
        return f"projects/{task['project_id']}/task/{task['id']}"
    if task["course_id"]:
        return f"academia/{task['course_id']}/task/{task['id']}"
    return f"task/{task['id']}"


def note_link(note):
    if note["project_id"]:
        return f"projects/{note['project_id']}/note/{note['id']}"
    return f"academia/{note['course_id']}/note/{note['id']}"


def _streak(days, grace=0):
    """Consecutive days ending today (or yesterday, so an unfinished day doesn't break it). `grace` allows rest days."""
    days = set(days)
    cursor = today()
    if cursor.isoformat() not in days:
        cursor -= timedelta(days=1)
    count, misses = 0, 0
    while True:
        if cursor.isoformat() in days:
            count += 1
            misses = 0
        else:
            misses += 1
            if misses > grace:
                return count
        cursor -= timedelta(days=1)
        if count == 0 and misses > grace:
            return 0


def _next_birthday(value):
    if not value:
        return None
    try:
        month, day = (int(p) for p in str(value).split("-")[-2:])
        now = today()
        for year in (now.year, now.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                candidate = date(year, 3, 1)  # 29 Feb outside leap years.
            if candidate >= now:
                return candidate.isoformat()
    except ValueError:
        return None
