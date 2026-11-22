"""Read-only scan of a project's folder: media by type, DAW sessions, git state, code languages and word counts."""

from collections import Counter
import os
from pathlib import Path
import re
import subprocess
import time

SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist", ".next", "target", ".cache",
        "Backup", "Ableton Project Info", ".DS_Store", "Pods", "DerivedData", ".idea", ".vscode"}
MEDIA = {
    "audio": {".wav", ".aif", ".aiff", ".mp3", ".flac", ".m4a", ".ogg", ".alac", ".caf"},
    "session": {".als", ".logicx", ".flp", ".ptx", ".rpp", ".song", ".bwproject", ".cpr", ".band", ".reason"},
    "midi": {".mid", ".midi"},
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".svg", ".tif", ".tiff"},
    "video": {".mp4", ".mov", ".m4v", ".webm"},
    "design": {".fig", ".psd", ".sketch", ".ai", ".afdesign", ".blend", ".xd", ".procreate"},
    "document": {".md", ".markdown", ".txt", ".fountain", ".docx", ".pages", ".pdf", ".rtf", ".org", ".tex"},
}
CODE = {".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
        ".swift": "Swift", ".rs": "Rust", ".go": "Go", ".java": "Java", ".kt": "Kotlin", ".c": "C", ".h": "C",
        ".cpp": "C++", ".hpp": "C++", ".cc": "C++", ".m": "Objective-C", ".rb": "Ruby", ".cs": "C#",
        ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".sql": "SQL", ".sh": "Shell", ".lua": "Lua",
        ".dart": "Dart", ".php": "PHP", ".zig": "Zig", ".vue": "Vue", ".svelte": "Svelte"}
WORDS = {".md", ".markdown", ".txt", ".fountain", ".org", ".tex"}
MAX_FILES = 4000
MAX_DEPTH = 6
MAX_LISTED = 80


def kind_of(path):
    suffix = Path(path).suffix.lower()
    if Path(path).is_dir() and suffix not in MEDIA["session"]:
        return None
    for kind, suffixes in MEDIA.items():
        if suffix in suffixes:
            return kind
    if suffix in CODE:
        return "code"
    return "other"


def _walk(root):
    count = 0
    root_depth = len(root.parts)
    for folder, dirs, files in os.walk(root):
        here = Path(folder)
        depth = len(here.parts) - root_depth
        # Logic packages are folders; treat them as one file and don't descend.
        sessions = [d for d in dirs if Path(d).suffix.lower() in MEDIA["session"]]
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".") and d not in sessions
                   and depth < MAX_DEPTH]
        for name in sessions + files:
            if name.startswith(".") or name in SKIP:
                continue
            count += 1
            if count > MAX_FILES:
                return
            yield here / name


def scan(folder):
    root = Path(folder).expanduser()
    if not str(folder).strip() or not root.is_dir():
        return {"ok": False, "reason": "missing_folder"}
    started = time.monotonic()
    groups, languages, words, total, truncated = {}, Counter(), [], 0, False
    for path in _walk(root):
        total += 1
        if total >= MAX_FILES:
            truncated = True
        try:
            stat = path.stat()
        except OSError:
            continue
        kind = kind_of(path) if not path.is_dir() else "session"
        if kind == "code":
            languages[CODE[path.suffix.lower()]] += stat.st_size
        entry = {"path": str(path), "name": path.name, "rel": str(path.relative_to(root)), "size": stat.st_size,
                 "modified": stat.st_mtime, "kind": kind}
        if path.suffix.lower() in WORDS and stat.st_size < 4_000_000:
            try:
                count = len(re.findall(r"\b\w[\w'’-]*\b", path.read_text(errors="ignore")))
            except OSError:
                count = 0
            entry["words"] = count
            words.append(entry)
        groups.setdefault(kind, []).append(entry)
    for kind, items in groups.items():
        items.sort(key=lambda e: e["modified"], reverse=True)
        groups[kind] = items[:MAX_LISTED]
    lang_total = sum(languages.values()) or 1
    return {
        "ok": True, "folder": str(root), "files": total, "truncated": truncated,
        "groups": groups, "counts": {k: len(v) for k, v in groups.items()},
        "languages": [{"name": n, "share": b / lang_total} for n, b in languages.most_common(6)],
        "words": sum(w.get("words", 0) for w in words),
        "documents": sorted(words, key=lambda w: w["modified"], reverse=True)[:30],
        "git": git(root), "latest": max((e for items in groups.values() for e in items),
                                        key=lambda e: e["modified"], default=None),
        "elapsed": round(time.monotonic() - started, 3),
    }


def git(root):
    if not (Path(root) / ".git").exists():
        return None

    def run(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""

    try:
        branch = run("rev-parse", "--abbrev-ref", "HEAD")
        changes = [line for line in run("status", "--porcelain").splitlines() if line]
        log = run("log", "-12", "--pretty=format:%h%x1f%s%x1f%ct%x1f%an")
        week = run("rev-list", "--count", "--since=7.days", "HEAD")
        remote = run("config", "--get", "remote.origin.url")
    except (OSError, subprocess.TimeoutExpired):
        return None
    commits = []
    for line in log.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append({"hash": parts[0], "subject": parts[1], "at": int(parts[2]), "author": parts[3]})
    return {"branch": branch, "changes": len(changes), "commits": commits, "week": int(week or 0),
            "remote": _web_remote(remote)}


def _web_remote(remote):
    match = re.match(r"(?:git@|https://)([^:/]+)[:/](.+?)(?:\.git)?$", remote or "")
    return f"https://{match.group(1)}/{match.group(2)}" if match else ""
