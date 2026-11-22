"""Canvas (Quercus by default): the pinned canvas-mcp in its own Python 3.13, a personal access token in Keychain, and a
read-only registration in mcp.toml. The school's Canvas address and the Mac's timezone go in the server's environment,
where the dashboard's sync (src/dashboard/canvas.py) reads them too."""

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from src import resources
from src.integrations.auth import APITokenAuthAdapter
from src.install import InstallError
from src.paths import HOME

VERSION = "1.18.0"
DATA = HOME
SERVER = DATA / "servers/canvas-mcp"
EXECUTABLE = SERVER / ".venv/bin/canvas-mcp-server"
DEFAULT_SITE = "https://q.utoronto.ca"
# Read tools only: what Luna may use when you ask, and what the dashboard's sync reads (src/dashboard/canvas.py).
TOOLS = ["list_courses", "get_course_details", "get_syllabus", "get_course_content_overview", "list_assignments",
         "get_assignment_details", "list_quizzes", "get_quiz_details", "get_my_upcoming_assignments",
         "get_my_submission_status", "get_my_course_grades", "get_my_todo_items", "list_announcements",
         "list_my_announcements", "get_discussion_topic_details", "list_calendar_events", "list_modules",
         "list_module_items", "list_pages", "get_page_details"]
DESCRIPTION = "Quercus (Canvas): courses, assignments, quizzes, announcements, grades and course pages"


def local_timezone():
    """The Mac's IANA timezone (from /etc/localtime), or Toronto's."""
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "America/Toronto"


def environment(site=DEFAULT_SITE, timezone=None):
    """Fixed, non-secret settings: student role, no Canvas writes at all (over stdio an unset ALLOWED_WRITE_TOOLS
    exposes every write tool), no code sandbox, anonymised classmates."""
    return {"CANVAS_API_URL": site.rstrip("/") + "/api/v1", "CANVAS_ROLE": "student", "ALLOWED_WRITE_TOOLS": "none",
            "STUDENT_WRITE_TOOLS": "", "EXECUTE_TYPESCRIPT_ENABLED": "false", "ENABLE_DATA_ANONYMIZATION": "true",
            "LOG_REDACT_PII": "true", "TIMEZONE": timezone or local_timezone()}


ENV = environment(timezone="America/Toronto")


def site_address(text):
    """"q.utoronto.ca" or "https://q.utoronto.ca/courses" → "https://q.utoronto.ca"; None if it isn't an address."""
    text = (text or "").strip()
    if not text:
        return DEFAULT_SITE
    parts = urlsplit(text if "://" in text else "https://" + text)
    if parts.scheme != "https" or not parts.hostname or "." not in parts.hostname:
        return None
    return f"https://{parts.hostname}" + (f":{parts.port}" if parts.port else "")


def site():
    """(address, timezone) the Canvas integration was set up with, or Quercus and Toronto."""
    try:
        entry = tomllib.loads((DATA / "mcp.toml").read_text()).get("integrations", {}).get("canvas", {})
        env = entry.get("env", {})
        address = env.get("CANVAS_API_URL", "").removesuffix("/api/v1") or DEFAULT_SITE
        return address, env.get("TIMEZONE") or "America/Toronto"
    except (OSError, ValueError):
        return DEFAULT_SITE, "America/Toronto"


def adapter():
    return APITokenAuthAdapter("api-token:canvas", "CANVAS_API_TOKEN")


def install_server():
    if EXECUTABLE.exists():
        return
    uv = resources.uv()
    if not uv:
        raise InstallError("tools_missing")
    SERVER.mkdir(parents=True, exist_ok=True)
    for command in ([str(uv), "venv", "--python", "3.13", str(SERVER / ".venv")],
                    [str(uv), "pip", "install", "--quiet", f"canvas-mcp=={VERSION}"]):
        if subprocess.run(command, capture_output=True, env=os.environ | {"VIRTUAL_ENV": str(SERVER / ".venv")}).returncode:
            raise InstallError("install_failed")


def register(config_path=None, env=None):
    """Add the entry, or bring an existing one's description and tools up to date (and its settings, when setup passes
    new ones: a refresh alone keeps the school and timezone it has). Returns what happened."""
    config_path = config_path or DATA / "mcp.toml"
    existing = config_path.read_text() if config_path.exists() else ""
    line = "env = { " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in (env or ENV).items()) + " }"
    if "canvas" in tomllib.loads(existing).get("integrations", {}):
        head, _, rest = existing.partition("[integrations.canvas]")
        section, sep, tail = rest.partition("\n[")
        section = re.sub(r"(?m)^description = .*$", f"description = {json.dumps(DESCRIPTION)}", section)
        section = re.sub(r"(?m)^tools = .*$", f"tools = {json.dumps(TOOLS)}", section)
        if env is not None:
            section = re.sub(r"(?m)^env = .*$", lambda _: line, section)
        updated = head + "[integrations.canvas]" + section + sep + tail
        tomllib.loads(updated)
        if updated == existing:
            return "unchanged"
        config_path.write_text(updated)
        return "updated"
    entry = ("\n[integrations.canvas]\n"
             f"description = {json.dumps(DESCRIPTION)}\n"
             f"command = {json.dumps(str(EXECUTABLE))}\n"
             f"cwd = {json.dumps(str(SERVER))}\n"  # canvas-mcp loads a .env from its working directory; keep it empty
             'auth = "api_token"\ntoken_env = "CANVAS_API_TOKEN"\n'
             f"tools = {json.dumps(TOOLS)}\n"
             f"{line}\n")
    tomllib.loads(existing + entry)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(existing + entry)
    return "registered"


def test(env=None):
    """canvas-mcp's own connection check, with the stored token. True when Canvas accepted it."""
    env = os.environ | (env or ENV) | adapter().env()
    result = subprocess.run([str(EXECUTABLE), "--test"], cwd=SERVER, env=env, capture_output=True, text=True, timeout=60)
    return result.returncode == 0


def ready():
    try:
        entries = tomllib.loads((DATA / "mcp.toml").read_text()).get("integrations", {})
        return "canvas" in entries and bool(adapter().env())
    except Exception:  # noqa: BLE001  (no config or no token reads as not set up)
        return False


def install(job, address="", token=""):
    """The app's Canvas step: the server, the token (kept only if Canvas accepts it), the registration."""
    site_url = site_address(address)
    if site_url is None:
        raise InstallError("canvas_address_invalid")
    if not token.strip():
        raise InstallError("token_missing")
    job.report("Installing the Canvas connector")
    install_server()
    job.report("Checking your token with Canvas")
    env = environment(site_url)
    keeper = adapter()
    # Setup may ask: a token saved by another copy of Luna needs your OK before this one can read or replace it.
    keeper.storage._interactive.set(True)
    previous = keeper.storage._read().get("token")
    keeper.save(token)
    if not test(env):
        if previous:
            keeper.save(previous)
        else:
            keeper.storage._write({})
        raise InstallError("token_rejected")
    register(env=env)
