"""One inbox over iMessage (read from Messages' own database) and Beeper (Instagram, LinkedIn, Discord and more).

Both sources are the apps' live state, so replies sent from the original apps show up here on the next poll.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import glob
import inspect
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from urllib.parse import quote

import aiohttp
from loguru import logger

from src.paths import keychain_service

CHAT_DB = Path.home() / "Library/Messages/chat.db"
ATTACHMENTS = Path.home() / "Library/Messages/Attachments"
ADDRESS_BOOK = Path.home() / "Library/Application Support/AddressBook"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
# Beeper Server (headless, `beeper setup --server`) first, then Beeper Desktop: both serve the same API.
BEEPER_URLS = ("http://127.0.0.1:23374", "http://127.0.0.1:23373")
KEYCHAIN_SERVICE = keychain_service("Luna Dashboard")
TAPBACKS = {2000: "❤️", 2001: "👍", 2002: "👎", 2003: "😂", 2004: "‼️", 2005: "❓"}
NETWORK_SLUGS = {"imessage": "imessage", "sms": "sms", "rcs": "sms", "discord": "discord", "instagram": "instagram",
                 "whatsapp": "whatsapp", "telegram": "telegram", "signal": "signal", "messenger": "messenger",
                 "slack": "slack", "x": "x", "twitter": "x", "linkedin": "linkedin", "google messages": "sms",
                 "beeper": "beeper", "beeper (matrix)": "beeper"}


class ProviderError(Exception):
    """`code` is a safe, user-explainable reason; provider responses never travel in it."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def apple_time(value):
    if not value:
        return None
    seconds = value / 1e9 if value > 1e12 else value
    return (APPLE_EPOCH + timedelta(seconds=seconds)).astimezone().isoformat(timespec="seconds")


def decode_attributed_body(blob):
    """Plain text from an NSAttributedString typedstream, which newer macOS stores instead of `message.text`."""
    if not blob:
        return ""
    blob = bytes(blob)
    at = blob.find(b"NSString")
    if at < 0:
        return ""
    plus = blob.find(b"\x2b", at + 8, at + 24)
    if plus < 0:
        return ""
    cursor = plus + 1
    length = blob[cursor]
    cursor += 1
    if length == 0x81:
        length = int.from_bytes(blob[cursor:cursor + 2], "little")
        cursor += 2
    elif length == 0x82:
        length = int.from_bytes(blob[cursor:cursor + 4], "little")
        cursor += 4
    return blob[cursor:cursor + length].decode("utf-8", "replace")


def _digits(handle):
    digits = re.sub(r"\D", "", handle or "")
    return digits[-10:] if len(digits) >= 7 else ""


def handle_key(handle):
    handle = (handle or "").strip().lower()
    return handle if "@" in handle else _digits(handle)


def network_slug(name):
    name = (name or "").strip().lower()
    return NETWORK_SLUGS.get(name, re.sub(r"[^a-z0-9]+", "", name) or "other")


# Keychain --------------------------------------------------------------------------------------------------------

def _keychain_query(account):
    import Security as S
    from src.paths import PROFILE

    # A separate home (LUNA_HOME) keeps its own secrets, so trying Luna as a new user never reads or replaces yours.
    account = account + (f"#{PROFILE}" if PROFILE else "")
    return {S.kSecClass: S.kSecClassGenericPassword, S.kSecAttrService: KEYCHAIN_SERVICE, S.kSecAttrAccount: account}


def read_secret(account):
    import Security as S
    status, data = S.SecItemCopyMatching(
        _keychain_query(account) | {S.kSecReturnData: True, S.kSecMatchLimit: S.kSecMatchLimitOne}, None)
    return bytes(data).decode() if status == S.errSecSuccess else None


def write_secret(account, value):
    import Security as S
    from Foundation import NSData
    if not value:
        S.SecItemDelete(_keychain_query(account))
        return
    payload = value.encode()
    data = NSData.dataWithBytes_length_(payload, len(payload))
    status = S.SecItemUpdate(_keychain_query(account), {S.kSecValueData: data})
    if status == S.errSecItemNotFound:
        status, _ = S.SecItemAdd(_keychain_query(account) | {S.kSecValueData: data}, None)
    if status != S.errSecSuccess:
        raise ProviderError("keychain_unavailable")


# Contacts --------------------------------------------------------------------------------------------------------

def load_contacts(root=ADDRESS_BOOK):
    """Handle → name from the local Contacts databases (needs the same Full Disk Access as Messages)."""
    names = {}
    for path in glob.glob(str(root / "Sources/*/AddressBook-v22.abcddb")) + [str(root / "AddressBook-v22.abcddb")]:
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            people = {}
            for pk, first, last, org in db.execute("SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION FROM ZABCDRECORD"):
                name = " ".join(p for p in (first, last) if p) or org
                if name:
                    people[pk] = name
            for owner, value in db.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                if owner in people and _digits(value):
                    names.setdefault(_digits(value), people[owner])
            for owner, value in db.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if owner in people and value:
                    names.setdefault(value.lower(), people[owner])
        except sqlite3.Error:
            pass
        finally:
            db.close()
    return names


# iMessage --------------------------------------------------------------------------------------------------------

class IMessage:
    id = "imessage"
    label = "Messages"

    def __init__(self, path=CHAT_DB, contacts=None, send=None):
        self.path = Path(path)
        self._contacts = contacts
        self._send = send or _osascript_send
        self._stamp = None

    def _connect(self):
        if not self.path.exists():
            raise ProviderError("not_available")
        try:
            db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            db.execute("SELECT 1 FROM message LIMIT 1")
            return db
        except sqlite3.OperationalError as error:
            if "authorization denied" in str(error) or "unable to open" in str(error):
                raise ProviderError("full_disk_access") from None
            raise ProviderError("unreadable") from None

    @property
    def contacts(self):
        if self._contacts is None:
            self._contacts = load_contacts()
        return self._contacts

    def name(self, handle):
        return self.contacts.get(handle_key(handle)) or handle

    def status(self):
        try:
            self._connect().close()
            return {"id": self.id, "label": self.label, "state": "ready", "networks": ["imessage", "sms"]}
        except ProviderError as error:
            return {"id": self.id, "label": self.label, "state": error.code, "networks": ["imessage", "sms"]}

    def changed(self):
        """Cheap change detection: Messages writes through its WAL, so its size and mtime move on every message."""
        stamps = []
        for suffix in ("", "-wal"):
            try:
                stat = os.stat(f"{self.path}{suffix}")
                stamps.append((stat.st_mtime_ns, stat.st_size))
            except OSError:
                stamps.append(None)
        changed = self._stamp is not None and stamps != self._stamp
        self._stamp = stamps
        return changed

    def chats(self, limit=80):
        db = self._connect()
        try:
            latest = db.execute(
                "SELECT cmj.chat_id, MAX(m.ROWID) AS last_id FROM chat_message_join cmj "
                "JOIN message m ON m.ROWID = cmj.message_id WHERE m.associated_message_type = 0 "
                "GROUP BY cmj.chat_id ORDER BY MAX(m.date) DESC LIMIT ?", (limit,)).fetchall()
            chats = []
            for row in latest:
                chat = db.execute("SELECT * FROM chat WHERE ROWID = ?", (row["chat_id"],)).fetchone()
                last = db.execute("SELECT * FROM message WHERE ROWID = ?", (row["last_id"],)).fetchone()
                handles = [h["id"] for h in db.execute(
                    "SELECT h.id FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id "
                    "WHERE chj.chat_id = ?", (row["chat_id"],))]
                unread = db.execute(
                    "SELECT COUNT(*) FROM chat_message_join cmj JOIN message m ON m.ROWID = cmj.message_id "
                    "WHERE cmj.chat_id = ? AND m.is_read = 0 AND m.is_from_me = 0 AND m.item_type = 0 "
                    "AND m.associated_message_type = 0", (row["chat_id"],)).fetchone()[0]
                names = [self.name(h) for h in handles]
                title = chat["display_name"] or ", ".join(names[:4]) or chat["chat_identifier"]
                service = (chat["service_name"] or "iMessage").lower()
                chats.append({
                    "id": f"imessage:{chat['guid']}", "network": network_slug(service), "provider": self.id,
                    "title": title, "group": len(handles) > 1, "unread": unread,
                    "participants": [{"name": n, "handle": h} for n, h in zip(names, handles)],
                    "at": apple_time(last["date"]),
                    "preview": {"text": _message_text(last) or _attachment_hint(last), "from_me": bool(last["is_from_me"])},
                })
            return chats
        finally:
            db.close()

    def messages(self, chat_id, limit=120, before=None):
        guid = chat_id.split(":", 1)[1]
        db = self._connect()
        try:
            chat = db.execute("SELECT ROWID FROM chat WHERE guid = ?", (guid,)).fetchone()
            if chat is None:
                raise ProviderError("chat_not_found")
            clause, args = ("AND m.ROWID < ?", (before,)) if before else ("", ())
            rows = db.execute(
                "SELECT m.*, h.id AS handle FROM chat_message_join cmj JOIN message m ON m.ROWID = cmj.message_id "
                f"LEFT JOIN handle h ON h.ROWID = m.handle_id WHERE cmj.chat_id = ? {clause} "
                "ORDER BY m.date DESC LIMIT ?", (chat["ROWID"], *args, limit * 2)).fetchall()[::-1]
            byguid, out = {}, []
            for row in rows:
                kind = row["associated_message_type"] or 0
                if 2000 <= kind < 3000:
                    target = re.sub(r"^(?:p:\d+/|bp:)", "", row["associated_message_guid"] or "")
                    if target in byguid:
                        emoji = TAPBACKS.get(kind) or (row["associated_message_emoji"]
                                                        if "associated_message_emoji" in row.keys() else None) or "•"
                        byguid[target]["reactions"].append(
                            {"emoji": emoji, "from_me": bool(row["is_from_me"]),
                             "sender": "You" if row["is_from_me"] else self.name(row["handle"])})
                    continue
                if kind >= 3000 or row["item_type"] != 0:
                    continue
                message = {
                    "id": str(row["ROWID"]), "chat_id": chat_id, "from_me": bool(row["is_from_me"]),
                    "sender": "You" if row["is_from_me"] else self.name(row["handle"]),
                    "text": _message_text(row), "at": apple_time(row["date"]), "reactions": [],
                    "status": ("read" if row["date_read"] else "delivered" if row["is_delivered"] else "sent")
                    if row["is_from_me"] else None,
                    "attachments": self._attachments(db, row["ROWID"]) if row["cache_has_attachments"] else [],
                }
                byguid[row["guid"]] = message
                out.append(message)
            return out[-limit:]
        finally:
            db.close()

    def _attachments(self, db, message_id):
        out = []
        for a in db.execute("SELECT a.* FROM message_attachment_join maj JOIN attachment a ON a.ROWID = maj.attachment_id "
                            "WHERE maj.message_id = ?", (message_id,)):
            path = str(Path(a["filename"] or "").expanduser()) if a["filename"] else ""
            mime = a["mime_type"] or ""
            out.append({"name": a["transfer_name"] or Path(path).name, "mime": mime, "kind": mime.split("/")[0],
                        "url": f"/api/messages/attachment?path={quote(path)}" if path else ""})
        return out

    def last_contact(self):
        """Most recent message date per handle, for keeping People up to date."""
        db = self._connect()
        try:
            return {handle_key(h): apple_time(d)[:10] for h, d in db.execute(
                "SELECT h.id, MAX(m.date) FROM message m JOIN handle h ON h.ROWID = m.handle_id "
                "WHERE m.associated_message_type = 0 GROUP BY h.id") if handle_key(h) and d}
        finally:
            db.close()

    async def send(self, chat_id, text):
        await asyncio.to_thread(self._send, chat_id.split(":", 1)[1], text)


def _message_text(row):
    text = row["text"] or decode_attributed_body(row["attributedBody"])
    return text.replace("\ufffc", "").strip()


def _attachment_hint(row):
    return "Attachment" if row["cache_has_attachments"] else ""


def _osascript_send(guid, text):
    # Text and chat travel as argv, never interpolated into the script.
    script = ['on run argv', 'tell application "Messages" to send (item 1 of argv) to chat id (item 2 of argv)', 'end run']
    args = ["osascript"] + [part for line in script for part in ("-e", line)] + [text, guid]
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise ProviderError("automation_denied" if "-1743" in result.stderr else "send_failed")


# Beeper (Server or Desktop) --------------------------------------------------------------------------------------

class Beeper:
    id = "beeper"
    label = "Beeper"

    def __init__(self, token=None, url=None, skip_networks=("imessage",)):
        self.token = token
        self.urls = (url,) if url else BEEPER_URLS
        self.url = url
        self.skip = set(skip_networks)
        self._session = None
        self._accounts = None
        self._stamp = None

    async def _find(self):
        # /v1/info needs no token, so this only finds a running app; the token is checked on the real request.
        for url in self.urls:
            try:
                async with self._session.get(url + "/v1/info", timeout=aiohttp.ClientTimeout(total=2)) as response:
                    if response.status < 500:
                        return url
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
        raise ProviderError("not_running")

    async def _request(self, method, path, **kwargs):
        if not self.token:
            raise ProviderError("not_configured")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        if self.url is None:
            self.url = await self._find()
        try:
            async with self._session.request(method, self.url + path,
                                             headers={"Authorization": f"Bearer {self.token}"}, **kwargs) as response:
                if response.status in (401, 403):
                    raise ProviderError("token_rejected")
                if response.status >= 400:
                    raise ProviderError("request_failed")
                return await response.json()
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
            if len(self.urls) > 1:
                self.url = None  # The other app may be the one running next time.
            raise ProviderError("not_running") from None

    async def close(self):
        if self._session is not None:
            await self._session.close()

    async def status(self):
        base = {"id": self.id, "label": self.label, "networks": []}
        try:
            accounts = await self._request("GET", "/v1/accounts")
        except ProviderError as error:
            return base | {"state": error.code}
        self._accounts = {a["accountID"]: a for a in accounts}
        networks = sorted({network_slug(a.get("network")) for a in accounts} - self.skip)
        return base | {"state": "ready", "networks": networks,
                       "accounts": [{"network": network_slug(a.get("network")), "name": a.get("network"),
                                     "state": a.get("status")} for a in accounts]}

    async def chats(self, limit=80):
        data = await self._request("GET", "/v1/chats", params={"limit": str(min(limit, 200))})
        out = []
        for chat in data.get("items", []):
            network = network_slug(chat.get("network"))
            if network in self.skip or chat.get("isArchived"):
                continue
            preview = chat.get("preview") or {}
            participants = [{"name": p.get("fullName") or p.get("username") or "", "handle": p.get("username") or ""}
                            for p in (chat.get("participants") or {}).get("items", []) if not p.get("isSelf")]
            out.append({
                "id": f"beeper:{chat['id']}", "network": network, "provider": self.id,
                "title": chat.get("title") or ", ".join(p["name"] for p in participants[:4]) or "Chat",
                "group": chat.get("type") == "group", "unread": chat.get("unreadCount") or 0,
                "participants": participants, "at": chat.get("lastActivity"), "pinned": bool(chat.get("isPinned")),
                "muted": bool(chat.get("isMuted")),
                "preview": {"text": preview.get("text") or _beeper_hint(preview), "from_me": bool(preview.get("isSender"))},
            })
        return out

    async def messages(self, chat_id, limit=120, before=None):
        raw = chat_id.split(":", 1)[1]
        params = {"cursor": before, "direction": "before"} if before else {}
        data = await self._request("GET", f"/v1/chats/{quote(raw, safe='')}/messages", params=params)
        out = []
        for m in sorted(data.get("items", []), key=lambda m: m.get("timestamp") or ""):
            if m.get("isDeleted") or m.get("isHidden") or m.get("type") == "REACTION":
                continue
            out.append({
                "id": m["id"], "chat_id": chat_id, "from_me": bool(m.get("isSender")),
                "sender": "You" if m.get("isSender") else (m.get("senderName") or ""),
                "text": m.get("text") or "", "at": m.get("timestamp"), "status": m.get("sendStatus"),
                "reactions": [{"emoji": r.get("reactionKey") or "•", "from_me": bool(r.get("participantID") == "self"),
                               "sender": ""} for r in (m.get("reactions") or [])],
                "attachments": [{"name": a.get("fileName") or "", "mime": a.get("mimeType") or "",
                                 "kind": {"img": "image"}.get(a.get("type"), a.get("type") or "file"),
                                 "url": a.get("srcURL") if str(a.get("srcURL", "")).startswith("http") else ""}
                                for a in (m.get("attachments") or [])],
            })
        return out[-limit:]

    async def send(self, chat_id, text):
        raw = chat_id.split(":", 1)[1]
        await self._request("POST", f"/v1/chats/{quote(raw, safe='')}/messages", json={"text": text})

    async def mark_read(self, chat_id):
        raw = chat_id.split(":", 1)[1]
        await self._request("POST", f"/v1/chats/{quote(raw, safe='')}/read", json={})

    async def changed(self):
        data = await self._request("GET", "/v1/chats", params={"limit": "20"})
        stamp = [(c.get("id"), c.get("lastActivity"), c.get("unreadCount")) for c in data.get("items", [])]
        changed = self._stamp is not None and stamp != self._stamp
        self._stamp = stamp
        return changed


def _beeper_hint(preview):
    kind = (preview.get("type") or "").lower()
    return {"image": "Photo", "video": "Video", "voice": "Voice message", "audio": "Audio", "file": "File",
            "sticker": "Sticker", "location": "Location"}.get(kind, "")


# Hub -------------------------------------------------------------------------------------------------------------

class Hub:
    """Merges providers, keeps a short-lived chat cache, and announces changes."""

    def __init__(self, providers, publish=lambda event: None, interval=2.0):
        self.providers = {p.id: p for p in providers}
        self.publish = publish
        self.interval = interval

    def add(self, provider):
        self.providers[provider.id] = provider

    async def _call(self, provider, name, *args):
        method = getattr(provider, name)
        if inspect.iscoroutinefunction(method):
            return await method(*args)
        return await asyncio.to_thread(method, *args)

    async def status(self):
        return [await self._call(p, "status") for p in self.providers.values()]

    async def chats(self):
        chats, problems = [], {}
        for provider in self.providers.values():
            try:
                chats += await self._call(provider, "chats")
            except ProviderError as error:
                problems[provider.id] = error.code
        chats.sort(key=lambda c: c.get("at") or "", reverse=True)
        return {"chats": chats, "problems": problems}

    def _provider(self, chat_id):
        provider = self.providers.get(chat_id.split(":", 1)[0])
        if provider is None:
            raise ProviderError("chat_not_found")
        return provider

    async def messages(self, chat_id, before=None):
        provider = self._provider(chat_id)
        messages = await self._call(provider, "messages", chat_id, 120, before)
        if hasattr(provider, "mark_read"):
            try:
                await provider.mark_read(chat_id)
            except ProviderError:
                pass
        return messages

    async def send(self, chat_id, text):
        text = text.strip()
        if not text:
            raise ProviderError("empty_message")
        await self._call(self._provider(chat_id), "send", chat_id, text)
        self.publish({"type": "messages", "chat_id": chat_id})

    async def watch(self, stopped):
        while not stopped.is_set():
            for provider in list(self.providers.values()):
                try:
                    if await self._call(provider, "changed"):
                        self.publish({"type": "messages", "provider": provider.id})
                except ProviderError:
                    pass
                except Exception as error:  # A misbehaving provider must not stop the others.
                    logger.debug(f"Message watch skipped {provider.id}: {type(error).__name__}")
            try:
                await asyncio.wait_for(stopped.wait(), self.interval)
            except asyncio.TimeoutError:
                pass
