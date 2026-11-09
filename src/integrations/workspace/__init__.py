"""Google Workspace tool contracts backed by the public Google APIs."""

import json
from pathlib import Path

SCOPE_BASE = "https://www.googleapis.com/auth/"
SERVICES = {
    "gmail": (
        "gmailmcp",
        "Gmail messages and drafts",
        ["gmail.readonly", "gmail.compose"],
    ),
    "drive": ("drivemcp", "Google Drive files", ["drive.readonly", "drive.file"]),
    "docs": (
        "docsmcp",
        "Google Docs",
        ["drive.readonly", "drive.file", "documents.readonly", "documents"],
    ),
    "sheets": (
        "sheetsmcp",
        "Google Sheets",
        ["drive.readonly", "drive.file", "spreadsheets.readonly", "spreadsheets"],
    ),
    "slides": (
        "slidesmcp",
        "Google Slides",
        ["drive.readonly", "drive.file", "presentations.readonly", "presentations"],
    ),
    "calendar": (
        "calendarmcp",
        "Google Calendar",
        [
            "calendar.calendarlist.readonly",
            "calendar.events.freebusy",
            "calendar.events.readonly",
        ],
    ),
    "chat": (
        "chatmcp",
        "Google Chat",
        [
            "chat.spaces.readonly",
            "chat.memberships.readonly",
            "chat.messages.readonly",
            "chat.messages.create",
            "chat.users.readstate",
        ],
    ),
    "people": (
        "people",
        "Google contacts and profile",
        ["directory.readonly", "userinfo.profile", "contacts.readonly"],
    ),
}
WRITE_SCOPES = {
    "gmail": ["gmail.modify"],
    "drive": ["drive.file"],
    "docs": ["documents"],
    "sheets": ["spreadsheets"],
    "slides": ["presentations"],
    "calendar": ["calendar.events"],
    "chat": ["chat.messages.create"],
    "people": [],
}
TOOLS = json.loads(Path(__file__).with_name("tools.json").read_text())


def credential_resource(service):
    # Keep the existing service-bound Keychain records when changing transports.
    return f"https://{SERVICES[service][0]}.googleapis.com/mcp/v1"
