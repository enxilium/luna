"""Private local text-chat history; independent of the agent's persistent memory."""

import json
from pathlib import Path
import sqlite3
import time
import uuid


CHAT_PATH = Path.home() / "Library/Application Support/Luna/text-chats.sqlite3"


class ChatStore:
    def __init__(self, path=CHAT_PATH):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(path))
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys = ON")
            if str(path) != ":memory:":
                path.chmod(0o600)
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("This text-chat history needs a newer version of Luna.")
            with self.db:
                self.db.executescript("""
                    CREATE TABLE IF NOT EXISTS chats (
                        id TEXT PRIMARY KEY, title TEXT NOT NULL, updated REAL NOT NULL,
                        draft TEXT NOT NULL DEFAULT '', files TEXT NOT NULL DEFAULT '[]'
                    );
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id),
                        role TEXT NOT NULL CHECK(role IN ('user', 'luna')),
                        text TEXT NOT NULL, files TEXT NOT NULL DEFAULT '[]'
                    );
                    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """)
                columns = {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}
                if "status" not in columns:
                    self.db.execute("ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT 'complete'")
                    self.db.execute("ALTER TABLE messages ADD COLUMN model TEXT NOT NULL DEFAULT ''")
                self.db.execute("UPDATE messages SET status = 'interrupted' WHERE status = 'pending'")
                self.db.execute("PRAGMA user_version = 2")
            selected = self.db.execute("SELECT value FROM settings WHERE key = 'active'").fetchone()
            self.active = selected[0] if selected else None
            if not self.active or not self.db.execute("SELECT 1 FROM chats WHERE id = ?", (self.active,)).fetchone():
                self.active = None
                self.new()
        except BaseException:
            self.db.close()
            raise

    @property
    def current(self):
        row = dict(self.db.execute("SELECT * FROM chats WHERE id = ?", (self.active,)).fetchone())
        row["files"] = json.loads(row["files"])
        return row

    def select(self, chat_id):
        if not self.db.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone():
            raise ValueError("That conversation no longer exists.")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('active', ?)", (chat_id,))
        self.active = chat_id

    def new(self):
        if self.active:
            row = self.current
            if not self.messages() and not row["draft"] and not row["files"]:
                return self.active
        chat_id = str(uuid.uuid4())
        with self.db:
            self.db.execute("INSERT INTO chats (id, title, updated) VALUES (?, 'New chat', ?)", (chat_id, time.time()))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('active', ?)", (chat_id,))
        self.active = chat_id
        return chat_id

    def save_draft(self, text, files):
        with self.db:
            self.db.execute("UPDATE chats SET draft = ?, files = ? WHERE id = ?",
                            (text, json.dumps(files), self.active))

    def messages(self):
        rows = self.db.execute("SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (self.active,))
        return [dict(row, files=json.loads(row["files"])) for row in rows]

    def conversations(self):
        return [dict(row) for row in self.db.execute("""
            SELECT chats.*, (SELECT text FROM messages WHERE chat_id = chats.id ORDER BY id DESC LIMIT 1) AS preview,
                   (SELECT COUNT(*) FROM messages WHERE chat_id = chats.id) AS count
            FROM chats ORDER BY updated DESC, rowid DESC
        """)]

    def send(self, text, files, reply=""):
        if not text.strip() and not files:
            raise ValueError("The message is empty.")
        title = " ".join(text.split())[:64] or Path(files[0]).name
        first = not self.messages()
        with self.db:
            user = self.db.execute("INSERT INTO messages (chat_id, role, text, files) VALUES (?, 'user', ?, ?)",
                                   (self.active, text, json.dumps(files))).lastrowid
            assistant = self.db.execute("INSERT INTO messages (chat_id, role, text, status) VALUES (?, 'luna', ?, ?)",
                                        (self.active, reply, 'complete' if reply else 'pending')).lastrowid
            self.db.execute("UPDATE chats SET title = CASE WHEN ? THEN ? ELSE title END, updated = ?, draft = '', files = '[]' WHERE id = ?",
                            (first, title, time.time(), self.active))
        return user, assistant

    def update_reply(self, message_id, text, *, status="pending", model=""):
        with self.db:
            self.db.execute("UPDATE messages SET text = ?, status = ?, model = ? WHERE id = ? AND role = 'luna'",
                            (text, status, model, message_id))

    def close(self):
        self.db.close()
