"""Loopback HTTP server for the dashboard: static UI, JSON API, server-sent events and guarded file access."""

import argparse
import asyncio
from datetime import date, datetime, timedelta
import json
import mimetypes
import os
from pathlib import Path
import secrets
import subprocess
import time

from aiohttp import web
from loguru import logger

from src.dashboard import imports, notes as note_files, projects as project_scan, symbols
from src.dashboard.calendar import open_calendar
from src.dashboard.location import open_location
from src.dashboard.canvas import Quercus
from src.dashboard.picks import DemoPicks, Picks, must
from src.dashboard.weather import DemoWeather, Weather
from src.dashboard.hotkey import DEFAULT_SHORTCUT, SHORTCUTS
from src.dashboard.messages import ATTACHMENTS, Beeper, Hub, IMessage, ProviderError, handle_key, read_secret, write_secret
from src.dashboard.store import DASHBOARD_PATH, RSVPS, TASK_STATUSES, Store, TABLES, VIEWS, task_link, view_range
from src import setup

WEB = Path(__file__).parent / "web"
COOKIE = "luna_dashboard"
RAW_AUDIO = {"Stems", "stems", "Samples", "samples", "Recorded", "Freeze", "Audio Files", "Consolidated", "Imported"}
QUERCUS_EVERY = 15 * 60  # seconds between Quercus syncs

DEFAULTS = {"name": "", "units": "metric", "currency": "USD", "weekly_sessions": 4, "steps_goal": 8000,
            "sleep_goal": 480, "imessage": True, "networks_hidden": [],
            "glass": "frosted", "glass_tint": 0.32, "backdrop_folder": "", "backdrop_interval": 10,
            "overlay_shortcut": DEFAULT_SHORTCUT}
GLASS_STYLES = ("clear", "frosted", "dark")
BACKDROP_INTERVALS = (1, 5, 10, 30, 60)  # minutes per picture
BACKDROP_TYPES = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"}
PROVIDER_MESSAGES = {
    "full_disk_access": "Luna needs Full Disk Access to read Messages.",
    "not_available": "Messages isn't set up on this Mac.",
    "not_configured": "Add a Beeper token to see Instagram, LinkedIn and more.",
    "not_running": "Beeper isn't running.",
    "token_rejected": "Beeper didn't accept the token.",
    "automation_denied": "Allow Luna to control Messages in System Settings → Privacy & Security → Automation.",
    "send_failed": "The message couldn't be sent.",
    "request_failed": "The service couldn't complete that request.",
    "chat_not_found": "That conversation is no longer available.",
    "empty_message": "Type a message first.",
    "keychain_unavailable": "The Keychain couldn't save that token.",
    "unreadable": "Messages' history couldn't be read.",
}


class Events:
    """Fan-out of small JSON events to every open page."""

    def __init__(self):
        self.queues = set()
        self.loop = None

    def publish(self, event):
        if self.loop is None:
            return
        for queue in list(self.queues):
            self.loop.call_soon_threadsafe(queue.put_nowait, event)


class Dashboard:
    def __init__(self, store, hub, *, token=None, port=0, demo=False, calendar=None, location=None, weather=None, picks=None,
                 quercus=None):
        self.store, self.hub = store, hub
        self.calendar = calendar or open_calendar(demo)
        self.location = location or open_location(demo, main_thread=False)
        self.weather = weather or (DemoWeather() if demo else Weather(
            self.location, lambda: self.store.setting("units", "metric"),
            on_place=lambda lat, lon: self.store.set_setting("weather_place", {"lat": lat, "lon": lon, "at": time.time()})))
        self.picks = picks or (DemoPicks(store) if demo else Picks(store))
        self.quercus = quercus  # keeps courses in step with Quercus; None (demo, tests) means no sync
        self.token = token or secrets.token_urlsafe(24)
        self.port = port
        self.demo = demo
        self.events = Events()
        setup.download.on_change = lambda snapshot: self.changed("setup", wake_model=snapshot)
        for job in setup.JOBS.values():
            job.on_change = lambda name, snapshot: self.changed("setup", connection=name, state=snapshot)
        self.scans = {}
        self.wallpaper_path = None
        self.stopped = asyncio.Event()
        hub.publish = self.events.publish

    # Plumbing ---------------------------------------------------------------------------------------------------

    def app(self):
        app = web.Application(middlewares=[self.guard], client_max_size=64 * 1024 * 1024)
        r = app.router
        r.add_get("/", self.index)
        r.add_static("/static", WEB, append_version=False)
        r.add_get("/sym/{name}", self.symbol)
        r.add_get("/api/events", self.stream)
        r.add_get("/api/overview", self.json(lambda q: self.store.overview()))
        r.add_get("/api/alerts", self.json(lambda q: self.store.alerts()))
        r.add_get("/api/day", self.day)
        r.add_get("/api/calendar", self.calendar_status)
        r.add_post("/api/calendar/access", self.calendar_access)
        r.add_get("/api/weather", lambda request: web.json_response(self.weather.hourly(), dumps=_dumps))
        r.add_get("/api/location", lambda request: web.json_response({"status": self.location.status()}))
        r.add_post("/api/location/access", self.location_access)
        r.add_get("/api/setup", self.json(lambda q: setup.status()))
        r.add_post("/api/setup/key", self.setup_key)
        r.add_post("/api/setup/microphone", self.setup_microphone)
        r.add_post("/api/setup/wake-model", self.setup_wake_model)
        r.add_post("/api/setup/connect/{name}", self.setup_connect)
        r.add_get("/api/wallpaper", self.wallpaper)
        r.add_get("/api/backdrop", self.backdrop)
        r.add_get("/api/backdrop/image", self.backdrop_image)
        r.add_get("/api/search", self.search)
        r.add_get("/api/settings", self.json(lambda q: self.settings()))
        r.add_put("/api/settings", self.put_settings)
        r.add_post("/api/quercus/sync", self.sync_quercus)
        r.add_get("/api/projects", self.json(lambda q: self.project_list()))
        r.add_get("/api/projects/{id}", self.project)
        r.add_post("/api/projects/{id}/scan", self.rescan)
        r.add_get("/api/courses", self.json(lambda q: self.store.courses()))
        r.add_get("/api/courses/{id}", self.course)
        r.add_get("/api/owners", self.json(lambda q: self.store.owners()))
        r.add_get("/api/tasks", self.json(lambda q: self.store.tasks(q.get("scope", "open"))))
        r.add_get("/api/tasks/{id}", self.task)
        r.add_patch("/api/tasks/{id}", self.update_task)
        r.add_post("/api/tasks/{id}/status", self.task_status)
        r.add_post("/api/tasks/{id}/complete", self.complete_task)
        r.add_post("/api/notes", self.create_note)
        r.add_get("/api/notes/{id}", self.note)
        r.add_patch("/api/notes/{id}", self.update_note)
        r.add_delete("/api/notes/{id}", self.delete_note)
        r.add_get("/api/media", self.media)
        r.add_post("/api/media", self.upload)
        r.add_post("/api/events", self.save_event)
        r.add_get("/api/events/{id}", self.event)
        r.add_patch("/api/events/{id}", self.save_event)
        r.add_post("/api/events/{id}/rsvp", self.rsvp)
        r.add_get("/api/habits", self.json(lambda q: self.store.habits(int(q.get("days", 84)))))
        r.add_post("/api/habits/{id}/toggle", self.toggle_habit)
        r.add_get("/api/journal", self.json(lambda q: self.store.journal(int(q.get("limit", 400)))))
        r.add_put("/api/journal/{day}", self.write_journal)
        r.add_get("/api/fitness", self.json(lambda q: self.store.fitness()))
        r.add_get("/api/workouts", self.json(lambda q: self.store.workouts(limit=int(q.get("limit", 60)))))
        r.add_post("/api/workouts", self.save_workout)
        r.add_patch("/api/workouts/{id}", self.save_workout)
        r.add_post("/api/metrics", self.set_metric)
        r.add_post("/api/import/health", self.import_health)
        r.add_post("/api/import/transactions", self.import_transactions)
        r.add_get("/api/money", self.json(lambda q: self.store.money(q.get("month"))))
        r.add_put("/api/budgets", self.set_budget)
        r.add_get("/api/people", self.json(lambda q: self.store.people()))
        r.add_post("/api/people/{id}/touch", self.touch_person)
        r.add_get("/api/messages/status", self.message_status)
        r.add_get("/api/messages/chats", self.chats)
        r.add_get("/api/messages/chats/{id}", self.chat)
        r.add_post("/api/messages/chats/{id}", self.send)
        r.add_get("/api/messages/attachment", self.attachment)
        r.add_put("/api/messages/beeper", self.set_beeper)
        r.add_get("/api/file", self.file)
        r.add_post("/api/open", self.open_path)
        r.add_post("/api/pick", self.pick)
        r.add_get("/api/{table}", self.list_records)
        r.add_post("/api/{table}", self.create_record)
        r.add_patch("/api/{table}/{id}", self.update_record)
        r.add_delete("/api/{table}/{id}", self.delete_record)
        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.cleanup)
        return app

    async def startup(self, app):
        self.events.loop = asyncio.get_running_loop()
        self.watcher = asyncio.create_task(self.hub.watch(self.stopped))
        self.background = asyncio.create_task(self.warm())
        self.weather_wake = asyncio.Event()
        loop = self.events.loop
        self.location.on_change = lambda: loop.call_soon_threadsafe(self.weather_wake.set)
        self.forecast = asyncio.create_task(self.keep_weather())
        self.planner = asyncio.create_task(self.keep_picks())
        self.quercus_loop = asyncio.create_task(self.keep_quercus())

    async def keep_weather(self):
        """Refresh the weather every 10 minutes (it fetches only when 30 minutes old), or as soon as you move."""
        while not self.stopped.is_set():
            if await self.weather.update():
                self.changed("weather")
            self.weather_wake.clear()
            try:
                await asyncio.wait_for(self.weather_wake.wait(), timeout=600)
            except TimeoutError:
                pass

    async def keep_picks(self):
        """Choose today's task picks when the day changes: checked every minute, so it happens just after midnight, or
        on waking or launching later that day."""
        while not self.stopped.is_set():
            try:
                if self.picks.due():
                    today = date.today()
                    events = await asyncio.to_thread(self.events_between, today, today)
                    if await self.picks.update(events):
                        self.changed("tasks")
            except Exception as error:  # noqa: BLE001  never let the planner stop; the next minute tries again
                logger.warning(f"Task picks check failed ({type(error).__name__})")
            try:
                await asyncio.wait_for(self.stopped.wait(), timeout=60)
            except TimeoutError:
                pass

    async def keep_quercus(self):
        """Read Quercus at launch and every 15 minutes: new work, moved deadlines, submissions, announcements."""
        while self.quercus and not self.stopped.is_set():
            await self.run_quercus()
            try:
                await asyncio.wait_for(self.stopped.wait(), timeout=QUERCUS_EVERY)
            except TimeoutError:
                pass

    async def run_quercus(self):
        try:
            status = await self.quercus.sync()
        except Exception as error:  # noqa: BLE001  a broken read must not stop the loop; the next one tries again
            logger.warning(f"Quercus sync failed ({type(error).__name__})")
            status = {"ok": False, "reason": "sync_failed"}
        if status.get("ok") and any(status.get(k) for k in ("created", "updated", "completed", "adopted")):
            self.changed("tasks")
        if status.get("ok") and (status.get("announcements") or status.get("removed")):
            self.changed("courses")
        self.changed("quercus")
        return status

    async def sync_quercus(self, request):
        if not self.quercus:
            return web.json_response({"ok": False, "reason": "not_set_up"})
        return web.json_response(await self.run_quercus(), dumps=_dumps)

    async def cleanup(self, app):
        self.stopped.set()
        for queue in list(self.events.queues):
            queue.put_nowait(None)
        for task in (self.watcher, self.background, self.forecast, self.planner, self.quercus_loop):
            task.cancel()
        for provider in self.hub.providers.values():
            if hasattr(provider, "close"):
                await provider.close()

    async def warm(self):
        """Scan project folders and refresh People's last contact without holding up the first paint."""
        for project in self.store.list("projects", "folder != ''"):
            await self.scan(project)
        await self.sync_people()

    @web.middleware
    async def guard(self, request, handler):
        host = request.host.split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            raise web.HTTPForbidden(text="Unknown host")
        if request.path == "/" and secrets.compare_digest(request.query.get("token", ""), self.token):
            response = web.HTTPFound("/")
            response.set_cookie(COOKIE, self.token, httponly=True, samesite="Strict")
            raise response
        if not secrets.compare_digest(request.cookies.get(COOKIE, ""), self.token):
            raise web.HTTPUnauthorized(text="Open the dashboard from Luna.")
        if request.method not in ("GET", "HEAD") and request.headers.get("Origin") not in (
                None, f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"):
            raise web.HTTPForbidden(text="Cross-origin request")
        try:
            response = await handler(request)
            if request.path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-cache"  # revalidate, so an update never mixes old and new modules
            return response
        except (ValueError, LookupError, KeyError) as error:
            if isinstance(error, KeyError):
                raise web.HTTPNotFound() from None
            return web.json_response({"error": str(error)}, status=400)
        except ProviderError as error:
            return web.json_response({"error": PROVIDER_MESSAGES.get(error.code, "That didn't work."),
                                      "code": error.code}, status=502)

    def json(self, compute):
        async def handler(request):
            return web.json_response(await asyncio.to_thread(compute, request.query), dumps=_dumps)
        return handler

    async def body(self, request):
        try:
            data = await request.json()
        except json.JSONDecodeError:
            raise ValueError("Malformed request.") from None
        if not isinstance(data, dict):
            raise ValueError("Malformed request.")
        return data

    async def wallpaper(self, request):
        """Only for the browser preview, which has no real desktop behind it."""
        path = self.wallpaper_path
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    def _backdrop_folder(self):
        folder = self.store.setting("backdrop_folder", "")
        path = Path(folder).expanduser() if folder else None
        return path if path and path.is_dir() else None

    async def backdrop(self, request):
        """The pictures the window's backdrop cycles through: images directly in the chosen folder, by name."""
        folder = self._backdrop_folder()
        names = sorted((p.name for p in folder.iterdir() if p.suffix.lower() in BACKDROP_TYPES and not p.name.startswith(".")),
                       key=str.lower)[:1000] if folder else []
        return web.json_response({"images": names, "interval": self.store.setting("backdrop_interval", 10)})

    async def backdrop_image(self, request):
        folder = self._backdrop_folder()
        if folder is None:
            raise web.HTTPNotFound()
        path = self._resolve(str(folder / request.query.get("name", "")), [folder])
        if path.suffix.lower() not in BACKDROP_TYPES or path.is_dir():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})

    async def index(self, request):
        html = (WEB / "index.html").read_text()
        html = html.replace("{{DEMO}}", "true" if self.demo else "false")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def symbol(self, request):
        name = request.match_info["name"]
        weight = request.query.get("w", "regular")
        data = await asyncio.to_thread(symbols.render, name, 64, weight)
        if data is None:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="image/png", headers={"Cache-Control": "max-age=86400"})

    async def stream(self, request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"})
        await response.prepare(request)
        queue = asyncio.Queue()
        self.events.queues.add(queue)
        try:
            await response.write(b"retry: 1500\n\n")
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 20)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                if event is None:
                    break
                await response.write(f"data: {_dumps(event)}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.events.queues.discard(queue)
        return response

    def changed(self, kind, **extra):
        self.events.publish({"type": kind, **extra})

    # Settings & search ------------------------------------------------------------------------------------------

    def settings(self):
        values = {key: self.store.setting(key, default) for key, default in DEFAULTS.items()}
        return values | {"demo": self.demo, "database": str(self.store.path), "quercus": self.store.setting("quercus_sync"),
                         "health_imported": self.store.setting("health_imported")}

    async def put_settings(self, request):
        data = await self.body(request)
        for key, value in data.items():
            if key not in DEFAULTS:
                raise ValueError(f"Unknown setting: {key}")
            if key == "glass" and value not in GLASS_STYLES:
                raise ValueError("Unknown glass style.")
            if key == "overlay_shortcut" and value not in SHORTCUTS:
                raise ValueError("Unknown shortcut.")
            if key == "glass_tint" and not 0 <= float(value) <= 0.8:
                raise ValueError("Use a tint from 0 to 0.8.")
            if key == "backdrop_folder" and value and not Path(str(value)).expanduser().is_dir():
                raise ValueError("That folder doesn't exist.")
            if key == "backdrop_interval" and value not in BACKDROP_INTERVALS:
                raise ValueError("Change pictures every 1, 5, 10, 30 or 60 minutes.")
            self.store.set_setting(key, value)
        if "units" in data:
            self.weather_wake.set()
        if "imessage" in data:
            self.configure_imessage()
        self.changed("settings")
        return web.json_response(self.settings())

    def configure_imessage(self):
        if self.demo:
            return
        if self.store.setting("imessage", True):
            self.hub.providers.setdefault("imessage", IMessage())
        else:
            self.hub.providers.pop("imessage", None)

    async def search(self, request):
        text = request.query.get("q", "")
        hits = await asyncio.to_thread(self.store.search, text)
        try:
            chats = (await self.hub.chats())["chats"]
        except ProviderError:
            chats = []
        needle = text.strip().lower()
        if len(needle) >= 2:
            hits += [{"kind": "chat", "id": c["id"], "title": c["title"], "network": c["network"]}
                     for c in chats if needle in c["title"].lower()][:6]
        return web.json_response(hits)

    # Generic records --------------------------------------------------------------------------------------------

    def _table(self, request):
        table = request.match_info["table"].replace("-", "_")
        if table not in TABLES or table == "workout_sets":
            raise web.HTTPNotFound()
        return table

    async def list_records(self, request):
        table = self._table(request)
        where, args = None, ()
        return web.json_response(await asyncio.to_thread(self.store.list, table, where, args, 500), dumps=_dumps)

    async def create_record(self, request):
        table = self._table(request)
        row = await asyncio.to_thread(self.store.create, table, await self.body(request))
        self.changed(table, id=row["id"])
        if table == "projects" and row["folder"]:
            asyncio.create_task(self.scan(row))
        return web.json_response(row, dumps=_dumps)

    async def update_record(self, request):
        table = self._table(request)
        row = await asyncio.to_thread(self.store.update, table, request.match_info["id"], await self.body(request))
        self.changed(table, id=row["id"])
        if table == "projects":
            asyncio.create_task(self.scan(row))
        return web.json_response(row, dumps=_dumps)

    async def delete_record(self, request):
        table = self._table(request)
        await asyncio.to_thread(self.store.delete, table, request.match_info["id"])
        self.changed(table, id=request.match_info["id"])
        return web.json_response({"ok": True})

    # Projects ---------------------------------------------------------------------------------------------------

    async def scan(self, project):
        if not project.get("folder"):
            self.scans.pop(project["id"], None)
            return None
        result = await asyncio.to_thread(project_scan.scan, project["folder"])
        result["at"] = time.time()
        self.scans[project["id"]] = result
        if result.get("ok") and result["words"]:
            history = self.store.setting(f"words:{project['id']}", {})
            history[date.today().isoformat()] = result["words"]
            self.store.set_setting(f"words:{project['id']}", dict(sorted(history.items())[-180:]))
        self.changed("scan", id=project["id"])
        return result

    def _summary(self, scan):
        if not scan or not scan.get("ok"):
            return scan and {"ok": False, "reason": scan.get("reason")}
        # The newest mix or bounce, not a stem or a recorded take that happens to be newer.
        audio = [a for a in scan["groups"].get("audio", []) if not set(Path(a["rel"]).parts[:-1]) & RAW_AUDIO]
        audio = audio or scan["groups"].get("audio", [])
        git = scan.get("git") or {}
        return {"ok": True, "counts": scan["counts"], "words": scan["words"], "languages": scan["languages"][:3],
                "latest": scan["latest"], "audio": audio[0] if audio else None,
                "cover": next(iter(scan["groups"].get("image", [])), None),
                "git": {"branch": git["branch"], "changes": git["changes"], "week": git["week"],
                        "last": git["commits"][0] if git["commits"] else None} if git else None}

    def project_list(self):
        rows = self.store.projects()
        for row in rows:
            row["scan"] = self._summary(self.scans.get(row["id"]))
        return rows

    async def project(self, request):
        project = await asyncio.to_thread(self.store.project, request.match_info["id"])
        if project is None:
            raise LookupError("That project no longer exists.")
        scan = self.scans.get(project["id"])
        if scan is None and project["folder"]:
            scan = await self.scan(project)
        project["scan"] = self._summary(scan)
        project["next"] = self._next_up(project)
        # The folder's newest files, whatever their kind, for "In the folder".
        files = sorted((f for group in (scan or {}).get("groups", {}).values() for f in group), key=lambda f: -f["modified"])
        project["recent"] = [{"name": f["name"], "path": f["path"], "modified": f["modified"], "kind": f["kind"]} for f in files[:4]]
        return web.json_response(project, dumps=_dumps)

    async def rescan(self, request):
        project = self.store.get("projects", request.match_info["id"])
        if project is None:
            raise LookupError("That project no longer exists.")
        return web.json_response(await self.scan(project) or {}, dumps=_dumps)

    # Courses, tasks, notes, media, events -----------------------------------------------------------------------

    async def course(self, request):
        course = await asyncio.to_thread(self.store.course, request.match_info["id"])
        if course is None:
            raise LookupError("That course no longer exists.")
        course["quercus"] = self.store.setting("quercus_sync") if self.quercus else None
        return web.json_response(course, dumps=_dumps)

    def _task(self, task_id):
        task = self.store.get("tasks", task_id)
        if task is None:
            raise LookupError("That task no longer exists.")
        kind, owner = note_files.owner_of(self.store, task)
        task["owner"] = owner and {"kind": kind, "id": owner["id"], "label": owner.get("title") or owner.get("code")}
        task["media"] = f"{kind}:{owner['id']}" if owner else "task"
        task["link"] = task_link(task)
        return task

    async def task(self, request):
        return web.json_response(await asyncio.to_thread(self._task, request.match_info["id"]), dumps=_dumps)

    async def update_task(self, request):
        """Edits from the task editor. A status change goes through set_task_status so repeats behave."""
        data = await self.body(request)
        status = data.pop("status", None)
        task_id = request.match_info["id"]
        if data:
            await asyncio.to_thread(self.store.update, "tasks", task_id, data)
        if status is not None:
            await asyncio.to_thread(self.store.set_task_status, task_id, status)
        self.changed("tasks", id=task_id)
        return web.json_response(await asyncio.to_thread(self._task, task_id), dumps=_dumps)

    async def task_status(self, request):
        status = (await self.body(request)).get("status")
        if status not in TASK_STATUSES:
            raise ValueError("Status is todo, doing or done.")
        row = await asyncio.to_thread(self.store.set_task_status, request.match_info["id"], status)
        self.changed("tasks", id=row["id"])
        return web.json_response(row, dumps=_dumps)

    def _note(self, note_id, body=True):
        note = self.store.get("notes", note_id)
        if note is None:
            raise LookupError("That note no longer exists.")
        kind, owner = note_files.owner_of(self.store, note)
        note["media"] = f"{kind}:{owner['id']}"
        note["owner"] = {"kind": kind, "id": owner["id"], "label": owner.get("title") or owner.get("code")}
        if body:
            note["body"] = note_files.read(note["path"])
        return note

    async def create_note(self, request):
        data = await self.body(request)
        title = str(data.get("title") or "Untitled").strip() or "Untitled"
        owner_kind = "project" if data.get("project_id") else "course" if data.get("course_id") else None
        owner = owner_kind and self.store.get(f"{owner_kind}s", data[f"{owner_kind}_id"])
        if owner is None:
            raise ValueError("A note belongs to a project or a course.")
        path = await asyncio.to_thread(note_files.new_path, note_files.root(self.store, owner_kind, owner), title)
        row = await asyncio.to_thread(self.store.create, "notes", {"title": title, f"{owner_kind}_id": owner["id"]}, {"path": str(path)})
        if data.get("body"):
            await asyncio.to_thread(note_files.write, path, str(data["body"]))
        self.changed("notes", id=row["id"])
        return web.json_response(await asyncio.to_thread(self._note, row["id"]), dumps=_dumps)

    async def note(self, request):
        return web.json_response(await asyncio.to_thread(self._note, request.match_info["id"]), dumps=_dumps)

    async def update_note(self, request):
        """Title and body save separately: the body goes to the Markdown file, a new title renames it."""
        data = await self.body(request)
        note = await asyncio.to_thread(self._note, request.match_info["id"], False)
        if "body" in data:
            await asyncio.to_thread(note_files.write, note["path"], str(data.pop("body")))
        if "title" in data:
            title = str(data["title"]).strip() or "Untitled"
            path = await asyncio.to_thread(note_files.rename, note["path"], title)
            await asyncio.to_thread(self.store.db_set, "notes", note["id"], path=str(path))
            data["title"] = title
        data = {k: v for k, v in data.items() if k == "title"}
        await asyncio.to_thread(self.store.update, "notes", note["id"], data)  # stamps `updated` even for a body-only save
        self.changed("notes", id=note["id"])
        return web.json_response(await asyncio.to_thread(self._note, note["id"], False), dumps=_dumps)

    async def delete_note(self, request):
        note = await asyncio.to_thread(self._note, request.match_info["id"], False)
        await asyncio.to_thread(note_files.trash, note["path"])
        await asyncio.to_thread(self.store.delete, "notes", note["id"])
        self.changed("notes", id=note["id"])
        return web.json_response({"ok": True})

    def _media_root(self, key):
        """`project:<id>`, `course:<id>` or `task` (tasks that belong to nothing)."""
        kind, _, owner_id = str(key).partition(":")
        if kind == "task":
            return note_files.root(self.store, "task", None)
        if kind not in ("project", "course"):
            raise LookupError("Unknown owner.")
        owner = self.store.get(f"{kind}s", owner_id)
        if owner is None:
            raise LookupError("That owner no longer exists.")
        return note_files.root(self.store, kind, owner)

    async def media(self, request):
        path = await asyncio.to_thread(note_files.resolve, self._media_root(request.query.get("owner", "")), request.query.get("path", ""))
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return web.FileResponse(path, headers={"Content-Type": kind, "Cache-Control": "private, max-age=3600"})

    async def upload(self, request):
        folder = self._media_root(request.query.get("owner", ""))
        data = await request.read()
        rel = await asyncio.to_thread(note_files.save_media, folder, request.query.get("name", ""), data)
        return web.json_response({"path": rel, "name": Path(rel).name, "size": len(data)})

    async def event(self, request):
        event = await asyncio.to_thread(self.store.event, request.match_info["id"])
        if event is None:
            raise LookupError("That event no longer exists.")
        return web.json_response(event, dumps=_dumps)

    async def save_event(self, request):
        data = await self.body(request)
        data.pop("rsvp", None)  # your answer moves only through /rsvp
        if "id" in request.match_info:
            row = await asyncio.to_thread(self.store.update, "events", request.match_info["id"], data)
        else:
            row = await asyncio.to_thread(self.store.create, "events", data)
        self.changed("calendar", id=row["id"])
        return web.json_response(self.store.event(row["id"]), dumps=_dumps)

    async def rsvp(self, request):
        answer = (await self.body(request)).get("rsvp")
        if answer not in RSVPS[2:]:
            raise ValueError("Answer going, maybe or no.")
        event = self.store.get("events", request.match_info["id"])
        if event is None or not event["rsvp"]:
            raise LookupError("That invitation no longer exists.")
        await asyncio.to_thread(self.store.update, "events", event["id"], {"rsvp": answer})
        self.changed("calendar", id=event["id"])
        return web.json_response(self.store.event(event["id"]), dumps=_dumps)

    # Plan, journal, fitness, money, people ----------------------------------------------------------------------

    async def complete_task(self, request):
        data = await self.body(request)
        row = await asyncio.to_thread(self.store.complete_task, request.match_info["id"], data.get("done", True))
        self.changed("tasks", id=row["id"])
        return web.json_response(row, dumps=_dumps)

    async def toggle_habit(self, request):
        data = await self.body(request)
        done = await asyncio.to_thread(self.store.toggle_habit, request.match_info["id"], data.get("day"))
        self.changed("habits")
        return web.json_response({"done": done})

    async def write_journal(self, request):
        row = await asyncio.to_thread(self.store.write_journal, request.match_info["day"], await self.body(request))
        self.changed("journal", day=row["day"])
        return web.json_response(row)

    async def save_workout(self, request):
        data = await self.body(request)
        row = await asyncio.to_thread(self.store.save_workout, data, request.match_info.get("id"))
        self.changed("workouts", id=row["id"])
        return web.json_response(row, dumps=_dumps)

    async def set_metric(self, request):
        data = await self.body(request)
        await asyncio.to_thread(self.store.set_metric, data.get("kind"), data.get("day"), data.get("value"))
        self.changed("metrics")
        return web.json_response({"ok": True})

    async def import_health(self, request):
        data = await self.body(request)
        path = Path(str(data.get("path", ""))).expanduser()
        result = await asyncio.to_thread(imports.import_health, self.store, path, data.get("since"))
        self.changed("workouts")
        return web.json_response(result)

    async def import_transactions(self, request):
        data = await self.body(request)
        if data.get("path"):
            text = Path(str(data["path"])).expanduser().read_text(errors="replace")
        else:
            text = str(data.get("text", ""))
        result = await asyncio.to_thread(imports.import_transactions, self.store, text, data.get("account_id"))
        self.changed("transactions")
        return web.json_response(result)

    async def set_budget(self, request):
        data = await self.body(request)
        await asyncio.to_thread(self.store.set_budget, str(data.get("category", "")).strip(), data.get("monthly"))
        self.changed("budgets")
        return web.json_response({"ok": True})

    async def touch_person(self, request):
        row = await asyncio.to_thread(self.store.touch_person, request.match_info["id"])
        self.changed("people", id=row["id"])
        return web.json_response(row, dumps=_dumps)

    async def sync_people(self):
        """Move each person's last contact forward from message history; never backwards, never invented."""
        provider = self.hub.providers.get("imessage")
        if provider is None or not hasattr(provider, "last_contact"):
            return
        try:
            seen = await asyncio.to_thread(provider.last_contact)
        except ProviderError:
            return
        changed = False
        for person in self.store.list("people"):
            days = [seen.get(handle_key(h["value"])) for h in person["handles"]]
            latest = max((d for d in days if d), default=None)
            if latest and (not person["last_contact"] or latest > person["last_contact"]):
                self.store.update("people", person["id"], {"last_contact": latest})
                changed = True
        if changed:
            self.changed("people")

    # Messages ---------------------------------------------------------------------------------------------------

    async def message_status(self, request):
        statuses = await self.hub.status()
        for status in statuses:
            status["message"] = PROVIDER_MESSAGES.get(status["state"]) if status["state"] != "ready" else None
        has_beeper = "beeper" in self.hub.providers and bool(getattr(self.hub.providers["beeper"], "token", None))
        return web.json_response({"providers": statuses, "beeper_configured": has_beeper or self.demo})

    # Home -------------------------------------------------------------------------------------------------------

    async def day(self, request):
        try:
            day = date.fromisoformat(request.query["date"]) if request.query.get("date") else date.today()
        except ValueError:
            raise ValueError("Use a date like 2026-11-14.") from None
        view = request.query.get("view", "day")
        if view not in VIEWS:
            raise ValueError("The view is day, week or month.")
        first, last = view_range(day, view)
        events = await asyncio.to_thread(self.events_between, first, last)
        plan = await asyncio.to_thread(self.store.day_plan, day, events, view)
        plan["calendar"] = self.calendar.status()
        plan["waiting"] = await asyncio.to_thread(self.invitations)
        near = await asyncio.to_thread(self.events_between, date.today(), date.today() + timedelta(days=1))
        plan["glance"] = await asyncio.to_thread(self.store.glance, near)
        plan["weather"] = self.weather.snapshot()
        # Luna's picks for today among upcoming tasks: anything due today or tomorrow, plus what the model chose.
        picks, today = self.picks.current(), date.today()
        for task in plan["tasks"]:
            if not task["late"]:
                task["pick"] = (("Due today" if task["due"] == today.isoformat() else "Due tomorrow") if must(task, today)
                                else picks.get(task["id"]))
        plan["done"] = await asyncio.to_thread(self.store.recently_done)
        plan["projects"] = self.home_projects()
        return web.json_response(plan, dumps=_dumps)

    def home_projects(self, limit=4):
        """Pick up where you left off: pinned projects, then the most recently touched open ones. Each kind brings the
        thing you'd reach for next: music the latest export to play, software the last commit, writing the word count and
        this week's progress, design the newest images, video the latest render. Without a folder there's nothing to read,
        so the tile asks for one in that kind's terms."""
        def touched(p):
            scan = self.scans.get(p["id"]) or {}
            scan = scan if scan.get("ok") else {}
            last = ((scan.get("git") or {}).get("commits") or [{}])[0]
            return max(p["updated"] or 0, p.get("last_task") or 0, (scan.get("latest") or {}).get("modified") or 0, last.get("at") or 0)

        rows = [p for p in self.store.projects() if not p["archived"]]
        pinned = sorted((p for p in rows if p["pinned"]), key=touched, reverse=True)
        recent = sorted((p for p in rows if not p["pinned"]), key=touched, reverse=True)
        return [{"id": p["id"], "title": p["title"], "category": p["category"], "category_label": p["category_label"],
                 "hue": p["hue"], "pinned": bool(p["pinned"]), "touched": touched(p), "next": self._next_up(p)}
                for p in (pinned + recent)[:limit]]

    @staticmethod
    def _commits(git):
        """The branch, its state, this week's count and the last two commits (the most a card shows)."""
        last = git["commits"][0]
        return {"kind": "commit", "branch": git["branch"], "subject": last["subject"], "hash": last["hash"], "at": last["at"],
                "changes": git["changes"], "week": git["week"],
                "commits": [{"hash": c["hash"], "subject": c["subject"], "at": c["at"]} for c in git["commits"][:2]]}

    def _next_up(self, p):
        if not p["folder"]:
            return {"kind": "link"}
        scan = self.scans.get(p["id"])
        if scan is None:
            return {"kind": "scanning"}
        if not scan.get("ok"):
            return {"kind": "missing"}
        groups, git = scan["groups"], scan.get("git")
        def latest(group):
            return groups.get(group, [None])[0]
        if p["category"] == "music":
            mixes = [a for a in groups.get("audio", []) if not set(Path(a["rel"]).parts[:-1]) & RAW_AUDIO] or groups.get("audio", [])
            if mixes:
                return {"kind": "audio", "file": mixes[0], "count": len(mixes)}
        if p["category"] == "software" and git and git["commits"]:
            return self._commits(git)
        if p["category"] == "writing" and scan["words"]:
            history = self.store.setting(f"words:{p['id']}", {})
            week_ago = (date.today() - timedelta(days=7)).isoformat()
            before = [v for d, v in sorted(history.items()) if d <= week_ago] or [v for d, v in sorted(history.items())]
            doc = (scan.get("documents") or [None])[0]
            return {"kind": "words", "words": scan["words"], "week": scan["words"] - before[-1] if before else None,
                    "name": doc["name"] if doc else None, "history": [v for _, v in sorted(history.items())][-14:]}
        if p["category"] == "photography" or p["category_label"].lower() == "design":
            images = [i for i in groups.get("image", []) if Path(i["name"]).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")]
            if images:
                return {"kind": "images", "files": images[:4], "count": len(groups.get("image", []))}
        if latest("video") and p["category_label"].lower() == "video":
            return {"kind": "file", "symbol": "film", "name": latest("video")["name"], "at": latest("video")["modified"]}
        if git and git["commits"]:  # any kind kept in git still has a last commit worth showing
            return self._commits(git)
        if scan.get("latest"):
            return {"kind": "file", "symbol": "doc", "name": scan["latest"]["name"], "at": scan["latest"]["modified"]}
        return {"kind": "empty"}

    def invitations(self, days=30, limit=8):
        """Invitations still waiting on you: upcoming events you haven't answered, or answered "maybe", soonest first."""
        now = datetime.now()
        events = self.events_between(now.date(), now.date() + timedelta(days=days))
        waiting = [e for e in events if e.get("rsvp") in ("pending", "maybe") and datetime.fromisoformat(e["end"]) > now]
        waiting.sort(key=lambda e: e["start"])
        return [{"id": e["id"], "title": e["title"], "start": e["start"], "all_day": e["all_day"], "rsvp": e["rsvp"],
                 "calendar": e["calendar"], "location": e["location"], "external_id": e.get("external_id", ""),
                 "source": e.get("source", "calendar")} for e in waiting[:limit]]

    def events_between(self, first, last):
        """Luna's own events and, until you've moved over, the ones macOS Calendar already has."""
        return self.calendar.events(first, last) + self.store.events_between(first, last)

    async def location_access(self, request):
        status = await asyncio.to_thread(self.location.request)
        self.weather_wake.set()
        self.changed("settings")
        return web.json_response({"status": status})

    # First-run setup (src/setup.py): what voice needs. The page shows it until both the key and the model are there.

    async def setup_key(self, request):
        result = await asyncio.to_thread(setup.save_gemini_key, str((await self.body(request)).get("key", "")))
        if result == "ok":
            self.changed("setup")
        return web.json_response({"result": result})

    async def setup_microphone(self, request):
        loop = asyncio.get_running_loop()
        answer = loop.create_future()
        setup.request_microphone(lambda state: loop.call_soon_threadsafe(lambda: answer.done() or answer.set_result(state)))
        return web.json_response({"microphone": await answer})

    async def setup_wake_model(self, request):
        setup.download.start()
        return web.json_response({"wake_model": setup.download.snapshot()})

    async def setup_connect(self, request):
        """Start memory, Google, Spotify or Canvas setup in the background; its steps arrive as `setup` events."""
        name = request.match_info["name"]
        job = setup.JOBS.get(name)
        if job is None or not await asyncio.to_thread(setup.AVAILABLE[name]):
            raise web.HTTPNotFound()
        data = await self.body(request)
        options = {"address": str(data.get("address", "")), "token": str(data.get("token", ""))} if name == "canvas" else {}
        job.start(**options)
        return web.json_response({"connection": await asyncio.to_thread(job.snapshot)})

    async def calendar_status(self, request):
        return web.json_response({"status": self.calendar.status()})

    async def calendar_access(self, request):
        status = await asyncio.to_thread(self.calendar.request)
        self.changed("settings")
        return web.json_response({"status": status})

    async def chats(self, request):
        result = await self.hub.chats()
        hidden = set(self.store.setting("networks_hidden", []))
        result["chats"] = [c for c in result["chats"] if c["network"] not in hidden]
        result["problems"] = {k: PROVIDER_MESSAGES.get(v, v) for k, v in result["problems"].items()}
        return web.json_response(result)

    async def chat(self, request):
        messages = await self.hub.messages(request.match_info["id"], request.query.get("before"))
        if request.match_info["id"].startswith("imessage:"):
            asyncio.create_task(self.sync_people())
        return web.json_response(messages)

    async def send(self, request):
        data = await self.body(request)
        await self.hub.send(request.match_info["id"], str(data.get("text", "")))
        return web.json_response({"ok": True})

    async def set_beeper(self, request):
        token = str((await self.body(request)).get("token", "")).strip()
        await asyncio.to_thread(write_secret, "beeper", token)
        if token:
            self.hub.add(Beeper(token, skip_networks=("imessage",) if "imessage" in self.hub.providers else ()))
        else:
            provider = self.hub.providers.pop("beeper", None)
            if provider is not None:
                await provider.close()
        self.changed("messages")
        return await self.message_status(request)

    async def attachment(self, request):
        return self._serve(request, request.query.get("path", ""), roots=[ATTACHMENTS])

    # Files ------------------------------------------------------------------------------------------------------

    def _allowed_roots(self):
        return [Path(r["folder"]).expanduser() for table in ("projects", "courses") for r in self.store.list(table, "folder != ''")]

    def _resolve(self, raw, roots):
        try:
            path = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            raise web.HTTPNotFound() from None
        for root in roots:
            try:
                root = root.resolve()
            except OSError:
                continue
            if path == root or path.is_relative_to(root):
                return path
        raise web.HTTPForbidden(text="That file isn't part of a project.")

    def _serve(self, request, raw, roots):
        path = self._resolve(raw, roots)
        if path.is_dir():
            raise web.HTTPNotFound()
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return web.FileResponse(path, headers={"Content-Type": kind, "Cache-Control": "no-cache"})

    async def file(self, request):
        return self._serve(request, request.query.get("path", ""), self._allowed_roots())

    async def open_path(self, request):
        data = await self.body(request)
        target = data.get("url") or ""
        if target:
            if not str(target).startswith(("https://", "http://", "x-apple.systempreferences:", "imessage:", "ical:",
                                           "discord:", "spotify:")):
                raise ValueError("Only web and app links can be opened.")
            await asyncio.to_thread(subprocess.run, ["open", target], timeout=10)
            return web.json_response({"ok": True})
        path = self._resolve(data.get("path", ""), self._allowed_roots())
        args = ["open", "-R", str(path)] if data.get("reveal") else ["open", str(path)]
        await asyncio.to_thread(subprocess.run, args, timeout=10)
        return web.json_response({"ok": True})

    async def pick(self, request):
        data = await self.body(request)
        kind = "folder" if data.get("kind") == "folder" else "file"
        prompt = str(data.get("prompt", "Choose"))[:80].replace('"', "'")
        script = f'POSIX path of (choose {kind} with prompt "{prompt}")'
        result = await asyncio.to_thread(subprocess.run, ["osascript", "-e", script],
                                         capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            return web.json_response({"path": None})
        return web.json_response({"path": result.stdout.strip().rstrip("/") or "/"})


def _default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def _dumps(value):
    return json.dumps(value, default=_default)


def build(*, demo=False, path=None):
    """The store and message hub for normal use, or a seeded throwaway pair for demos and UI work."""
    if demo:
        from src.dashboard import demo as demo_data

        store = Store(path or ":memory:")
        demo_data.seed(store)
        return store, Hub([demo_data.DemoMessages()])
    store = Store(path or DASHBOARD_PATH)
    providers = [IMessage()] if store.setting("imessage", True) else []
    try:
        token = read_secret("beeper")
    except Exception:
        token = None
    if token:
        providers.append(Beeper(token, skip_networks=("imessage",) if providers else ()))
    return store, Hub(providers)


async def serve(dashboard, *, host="127.0.0.1", ready=None, stop=None):
    # Open live-update streams never finish on their own; give them a moment, not the default minute.
    runner = web.AppRunner(dashboard.app(), access_log=None, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, host, dashboard.port)
    await site.start()
    dashboard.port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{dashboard.port}/?token={dashboard.token}"
    if ready:
        ready(url)
    try:
        await (stop.wait() if stop else asyncio.Event().wait())
    finally:
        await runner.cleanup()


def current_wallpaper():
    """A still of the desktop picture, so the browser preview has something to frost. Rotating folders give their first image."""
    import AppKit as AK

    screen = AK.NSScreen.mainScreen()
    url = AK.NSWorkspace.sharedWorkspace().desktopImageURLForScreen_(screen) if screen else None
    path = Path(str(url.path())) if url is not None else None
    if path and path.is_dir():
        images = sorted(p for p in path.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".heic", ".webp"))
        path = images[0] if images else None
    return str(path) if path and path.is_file() else None


def main():
    parser = argparse.ArgumentParser(description="Serve Luna's dashboard to a browser (for development)")
    parser.add_argument("--demo", action="store_true", help="Use seeded sample data in memory")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--token", default=None, help="Fixed token (development only)")
    args = parser.parse_args()
    store, hub = build(demo=args.demo)
    dashboard = Dashboard(store, hub, token=args.token, port=args.port, demo=args.demo,
                          quercus=None if args.demo else Quercus(store))
    dashboard.wallpaper_path = current_wallpaper()
    try:
        asyncio.run(serve(dashboard, ready=lambda url: print(f"Dashboard: {url}", flush=True)))
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
