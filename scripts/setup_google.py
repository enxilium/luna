"""Import a Google web client into Keychain and configure Workspace MCP services."""

import argparse
import asyncio
import json
import os
import tempfile
import tomllib
from pathlib import Path

from mcp.shared.auth import OAuthClientInformationFull

from src.integrations.auth import AuthFailure, KeychainTokenStorage


DATA = Path.home() / "Library/Application Support/Luna"
REDIRECT_URI = "http://127.0.0.1:8765/oauth/callback"
ISSUER = "https://accounts.google.com"
SCOPE_BASE = "https://www.googleapis.com/auth/"
SERVICES = {
    "gmail": ("gmailmcp", "Gmail messages and drafts", ["gmail.readonly", "gmail.compose"]),
    "drive": ("drivemcp", "Google Drive files", ["drive.readonly", "drive.file"]),
    "docs": ("docsmcp", "Google Docs", ["drive.readonly", "drive.file", "documents.readonly", "documents"]),
    "sheets": ("sheetsmcp", "Google Sheets", ["drive.readonly", "drive.file", "spreadsheets.readonly", "spreadsheets"]),
    "slides": ("slidesmcp", "Google Slides", ["drive.readonly", "drive.file", "presentations.readonly", "presentations"]),
    "calendar": ("calendarmcp", "Google Calendar", ["calendar.calendarlist.readonly", "calendar.events.freebusy", "calendar.events.readonly"]),
    "chat": ("chatmcp", "Google Chat", ["chat.spaces.readonly", "chat.memberships.readonly", "chat.messages.readonly", "chat.messages.create", "chat.users.readstate"]),
    "people": ("people", "Google contacts and profile", ["directory.readonly", "userinfo.profile", "contacts.readonly"]),
}


async def setup(source: Path) -> None:
    source = source.expanduser().resolve()
    try:
        payload = source.read_bytes()
        app = json.loads(payload)["web"]
        if (not isinstance(app, dict)
                or not isinstance(app.get("client_id"), str)
                or not app["client_id"].endswith(".apps.googleusercontent.com")
                or not isinstance(app.get("client_secret"), str) or not app["client_secret"]
                or app.get("token_uri") != "https://oauth2.googleapis.com/token"
                or not isinstance(app.get("redirect_uris"), list)
                or REDIRECT_URI not in app["redirect_uris"]):
            raise ValueError
        client = OAuthClientInformationFull(
            client_id=app["client_id"], client_secret=app["client_secret"], client_name="Luna",
            token_endpoint_auth_method="client_secret_post", redirect_uris=[REDIRECT_URI], issuer=ISSUER,
        )
    except (OSError, ValueError, TypeError, KeyError):
        raise AuthFailure("Expected a Google web-client JSON with Luna's registered loopback redirect") from None

    config_path = DATA / "mcp.toml"
    try:
        existing = config_path.read_text() if config_path.exists() else ""
        entries = tomllib.loads(existing).get("integrations", {})
        if not isinstance(entries, dict):
            raise ValueError
    except (OSError, ValueError):
        raise AuthFailure("Existing MCP configuration is invalid; it was left untouched") from None

    pending, stores = [], []
    for name, (host, description, scopes) in SERVICES.items():
        integration_id = f"google_{name}"
        url = f"https://{host}.googleapis.com/mcp/v1"
        entry = entries.get(integration_id)
        if entry is not None and (not isinstance(entry, dict) or entry.get("url") != url
                                  or entry.get("auth") != "mcp_oauth" or entry.get("client_id") != client.client_id):
            raise AuthFailure("Existing Google integration differs; it was left untouched")
        storage = KeychainTokenStorage(url)
        prior = await storage.get_client_info()
        if prior is not None and prior.client_id != client.client_id:
            raise AuthFailure("Existing Google Keychain client differs; it was left untouched")
        stores.append(storage)
        if entry is None:
            pending.append(
                f"\n[integrations.{integration_id}]\n"
                f"description = {json.dumps(description)}\nurl = {json.dumps(url)}\n"
                f'auth = "mcp_oauth"\nclient_id = {json.dumps(client.client_id)}\n'
                f"redirect_uri = {json.dumps(REDIRECT_URI)}\n"
                f"scopes = {json.dumps(' '.join(SCOPE_BASE + s for s in scopes))}\n"
                'authorization_params = { access_type = "offline", prompt = "consent" }\n'
            )

    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = DATA / "google-oauth-client.json"
    if source != destination.resolve():
        if destination.exists():
            if destination.read_bytes() != payload:
                raise AuthFailure("Protected Google credentials already exist; both files were left untouched")
        else:
            with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        os.chmod(destination, 0o600)
        source.unlink()
    else:
        os.chmod(destination, 0o600)
    for storage in stores:
        await storage.set_client_info(client)
    if pending:
        with tempfile.NamedTemporaryFile(mode="w", dir=DATA, delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(existing + "".join(pending))
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, config_path)
            finally:
                temporary.unlink(missing_ok=True)
    print("Google credentials are protected and imported into Keychain. Eight Workspace MCP services are configured.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("credentials", type=Path, help="Downloaded Google OAuth web-client JSON")
    try:
        asyncio.run(setup(parser.parse_args().credentials))
    except (AuthFailure, OSError):
        parser.exit(1, "Google setup failed. Check the credential file, redirect, Keychain, and existing configuration.\n")
