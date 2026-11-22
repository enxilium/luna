"""Notes as Markdown files, and the media that notes and task descriptions embed.

A project or course with a folder keeps its notes in `<folder>/Notes/` and their images and files in `Notes/assets/`, so
they stay readable (and Obsidian-compatible) without Luna. Without a folder they live under Luna's data folder. Tasks
that belong to nothing keep their media in `<data>/tasks/assets/`. Markdown links media relative to that root:
`![](assets/plot.png)`.
"""

import os
from pathlib import Path
import re
import shutil
import tempfile
import time

MAX_MEDIA = 50 * 1024 * 1024
UNSAFE = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')


def root(store, owner_kind, owner):
    """The folder holding an owner's notes and media. `owner` is a project or course row, or None for standalone tasks."""
    if owner is None:
        return store.files / "tasks"
    if owner.get("folder") and not getattr(store, "sandbox", False):  # the demo never writes into real folders
        return Path(owner["folder"]).expanduser() / "Notes"
    return store.files / "notes" / f"{owner_kind}-{owner['id']}"


def owner_of(store, row):
    """The project or course a task or note belongs to, as (kind, row), or ("task", None) for a standalone task."""
    if row.get("project_id"):
        return "project", store.get("projects", row["project_id"])
    if row.get("course_id"):
        return "course", store.get("courses", row["course_id"])
    return "task", None


def safe_name(name, fallback="Untitled"):
    clean = UNSAFE.sub(" ", str(name or "")).strip().strip(".")
    return " ".join(clean.split())[:80] or fallback


def _unique(folder, stem, suffix, skip=None):
    candidate, n = folder / f"{stem}{suffix}", 2
    while candidate.exists() and candidate != skip:
        candidate, n = folder / f"{stem} {n}{suffix}", n + 1
    return candidate


def new_path(folder, title):
    folder.mkdir(parents=True, exist_ok=True)
    path = _unique(folder, safe_name(title), ".md")
    path.touch()
    return path


def read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write(path, text):
    """Atomic: a crash or a full disk never leaves half a note."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".luna-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def rename(path, title):
    """Follow a renamed note on disk. Returns the new path (unchanged when the name already fits)."""
    path = Path(path)
    target = _unique(path.parent, safe_name(title), ".md", skip=path)
    if target != path:
        if path.exists():
            os.replace(path, target)
        else:
            target.touch()
    return target


def trash(path):
    """Deleted notes go to the Trash, not away: they're your files."""
    path = Path(path)
    if not path.exists():
        return
    bin_ = Path.home() / ".Trash"
    bin_.mkdir(exist_ok=True)
    shutil.move(path, _unique(bin_, path.stem, path.suffix))  # may cross volumes


def save_media(folder, name, data):
    """Store an upload under `assets/` with a safe, unused name. Returns the Markdown-relative path."""
    if len(data) > MAX_MEDIA:
        raise ValueError("That file is over 50 MB.")
    assets = Path(folder) / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    stem, suffix = os.path.splitext(safe_name(os.path.basename(str(name or "")), f"pasted-{int(time.time())}"))
    target = _unique(assets, stem or "file", suffix.lower()[:12])
    target.write_bytes(data)
    return f"assets/{target.name}"


def resolve(folder, rel):
    """A media path inside the owner's folder, or LookupError: links can't climb out of it."""
    base = Path(folder).resolve()
    try:
        path = (base / str(rel)).resolve(strict=True)
    except (OSError, RuntimeError):
        raise LookupError("That file is missing.") from None
    if not path.is_relative_to(base) or path.is_dir():
        raise LookupError("That file is missing.")
    return path
