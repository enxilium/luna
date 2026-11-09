"""Import a Google web client into Keychain and configure Workspace MCP services."""

import argparse
import asyncio
import json
import os
import re
import shutil
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path

from mcp.shared.auth import OAuthClientInformationFull

from src.integrations.auth import AuthFailure, KeychainTokenStorage
from src.integrations.workspace import SCOPE_BASE, SERVICES

DATA = Path.home() / "Library/Application Support/Luna"
REDIRECT_URI = "http://127.0.0.1:8765/oauth/callback"
ISSUER = "https://accounts.google.com"


def _owned_entry(name, entry):
    if not isinstance(entry, dict):
        return False
    host = SERVICES[name][0]
    return (
        entry.get("url") == f"https://{host}.googleapis.com/mcp/v1"
        and entry.get("auth") == "mcp_oauth"
    ) or (
        entry.get("auth") == "google_api"
        and entry.get("backend") == "workspace"
        and entry.get("service") == name
    )


def _toml(value):
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(json.dumps(k) + " = " + _toml(v) for k, v in value.items())
            + " }"
        )
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, (str, bool, int, float)):
        return json.dumps(value)
    raise AuthFailure(
        "Unsupported Workspace configuration value; config was left untouched"
    )


def use_backend(backend, config_path=None):
    """Change only owned Workspace tables; credentials and other integrations stay intact."""
    if backend not in {"local", "hosted"}:
        raise ValueError("Choose local or hosted")
    path = config_path or DATA / "mcp.toml"
    original = path.read_text()
    entries = tomllib.loads(original).get("integrations", {})
    replacements = {}
    for name in SERVICES:
        key = "google_" + name
        entry = entries.get(key)
        if not _owned_entry(name, entry):
            raise AuthFailure("Workspace configuration differs; it was left untouched")
        changed = {
            k: v
            for k, v in entry.items()
            if k not in {"url", "command", "args", "cwd", "auth", "service", "backend"}
        }
        if backend == "local":
            changed.update(
                backend="workspace",
                auth="google_api",
                service=name,
            )
        else:
            changed.update(
                url=f"https://{SERVICES[name][0]}.googleapis.com/mcp/v1",
                auth="mcp_oauth",
            )
        if changed != entry:
            replacements[key] = (
                f"[integrations.{key}]\n"
                + "".join(f"{k} = {_toml(v)}\n" for k, v in changed.items())
                + "\n"
            )
    if not replacements:
        return None
    updated = original
    for key, text in replacements.items():
        pattern = rf"(?ms)^\[integrations\.{re.escape(key)}\][ \t]*\n.*?(?=^\[|\Z)"
        updated, count = re.subn(pattern, lambda match, text=text: text, updated)
        if count != 1:
            raise AuthFailure(
                "Unsupported configuration layout; config was left untouched"
            )
    tomllib.loads(updated)
    backup = path.with_name(
        path.name
        + ".before-"
        + backend
        + "-"
        + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    )
    shutil.copy2(path, backup)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return backup


async def setup(source: Path, *, backend="local") -> None:
    source = source.expanduser().resolve()
    try:
        payload = source.read_bytes()
        app = json.loads(payload)["web"]
        if (
            not isinstance(app, dict)
            or not isinstance(app.get("client_id"), str)
            or not app["client_id"].endswith(".apps.googleusercontent.com")
            or not isinstance(app.get("client_secret"), str)
            or not app["client_secret"]
            or app.get("token_uri") != "https://oauth2.googleapis.com/token"
            or not isinstance(app.get("redirect_uris"), list)
            or REDIRECT_URI not in app["redirect_uris"]
        ):
            raise ValueError
        client = OAuthClientInformationFull(
            client_id=app["client_id"],
            client_secret=app["client_secret"],
            client_name="Luna",
            token_endpoint_auth_method="client_secret_post",
            redirect_uris=[REDIRECT_URI],
            issuer=ISSUER,
        )
    except (OSError, ValueError, TypeError, KeyError):
        raise AuthFailure(
            "Expected a Google web-client JSON with Luna's registered loopback redirect"
        ) from None

    config_path = DATA / "mcp.toml"
    try:
        existing = config_path.read_text() if config_path.exists() else ""
        entries = tomllib.loads(existing).get("integrations", {})
        if not isinstance(entries, dict):
            raise ValueError
    except (OSError, ValueError):
        raise AuthFailure(
            "Existing MCP configuration is invalid; it was left untouched"
        ) from None

    pending, stores = [], []
    for name, (host, description, scopes) in SERVICES.items():
        integration_id = f"google_{name}"
        url = f"https://{host}.googleapis.com/mcp/v1"
        entry = entries.get(integration_id)
        if entry is not None and (
            not _owned_entry(name, entry) or entry.get("client_id") != client.client_id
        ):
            raise AuthFailure(
                "Existing Google integration differs; it was left untouched"
            )
        storage = KeychainTokenStorage(url, interactive=True)
        prior = await storage.get_client_info()
        if prior is not None and prior.client_id != client.client_id:
            raise AuthFailure(
                "Existing Google Keychain client differs; it was left untouched"
            )
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
                raise AuthFailure(
                    "Protected Google credentials already exist; both files were left untouched"
                )
        else:
            with os.fdopen(
                os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            ) as output:
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
    use_backend(backend, config_path)
    print(
        f"Google credentials are protected in Keychain. Eight Workspace MCP services use the {backend} backend."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "credentials",
        type=Path,
        nargs="?",
        help="Downloaded Google OAuth web-client JSON",
    )
    parser.add_argument("--backend", choices=("local", "hosted"), default="local")
    try:
        args = parser.parse_args()
        if args.credentials:
            asyncio.run(setup(args.credentials, backend=args.backend))
        else:
            backup = use_backend(args.backend)
            print(
                f"Workspace uses the {args.backend} backend."
                + (f" Previous config: {backup}" if backup else "")
            )
    except (AuthFailure, OSError):
        parser.exit(
            1,
            "Google setup failed. Check the credential file, redirect, Keychain, and existing configuration.\n",
        )
