"""Google Workspace: import the OAuth Desktop app client people sign in through into Keychain, configure the eight Workspace
services in mcp.toml, and sign in once for all of them."""

import argparse
import asyncio
import json
import os
import re
import shutil
import tempfile
import time
import tomllib
from datetime import datetime
from pathlib import Path

from mcp.shared.auth import OAuthClientInformationFull

from src.integrations.auth import AuthFailure, KeychainTokenStorage
from src.integrations.workspace import SCOPE_BASE, SERVICES, credential_resource
from src.integrations.workspace.auth import GoogleAPIAuthAdapter
from src.paths import HOME

DATA = HOME
# A Desktop app client accepts any loopback port without registering it; Google documents the URI without a path.
REDIRECT_URI = "http://127.0.0.1:8765/"
SIGN_IN_SECONDS = 295
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


def _client(payload):
    try:
        # A Desktop app client is a public client: Google expects its secret to ship inside the app and the sign-in to
        # use PKCE, so bundling it gives nothing away. A Web application client's secret must stay on a server.
        app = json.loads(payload)["installed"]
        if (
            not isinstance(app, dict)
            or not isinstance(app.get("client_id"), str)
            or not app["client_id"].endswith(".apps.googleusercontent.com")
            or not isinstance(app.get("client_secret"), str)
            or not app["client_secret"]
            or app.get("token_uri") != "https://oauth2.googleapis.com/token"
        ):
            raise ValueError
        return OAuthClientInformationFull(
            client_id=app["client_id"],
            client_secret=app["client_secret"],
            client_name="Luna",
            token_endpoint_auth_method="client_secret_post",
            redirect_uris=[REDIRECT_URI],
            issuer=ISSUER,
        )
    except (ValueError, TypeError, KeyError):
        raise AuthFailure(
            "Expected a Google OAuth client JSON of type Desktop app"
        ) from None


async def setup(source: Path, *, backend="local") -> None:
    """Import a downloaded client file (the command line): the file moves into Luna's folder."""
    source = source.expanduser().resolve()
    try:
        payload = source.read_bytes()
    except OSError:
        raise AuthFailure(
            "Expected a Google OAuth client JSON of type Desktop app"
        ) from None
    await configure(payload, backend=backend)
    destination = DATA / "google-oauth-client.json"
    if source != destination.resolve():
        source.unlink()
    print(
        f"Google credentials are protected in Keychain. Eight Workspace MCP services use the {backend} backend."
    )


async def configure(payload, *, backend="local", replace=False) -> None:
    """Keep the client in Keychain for every Workspace service and register the services (idempotent).

    `replace` (the app's setup, with the client this Luna ships) swaps out a different client Luna set up earlier: its
    entries, file and Keychain record change over, and its tokens are dropped, since they belong to the old client.
    Without it (the command line), a different client is refused and nothing changes."""
    client = _client(payload)
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

    pending, stores, stale, swapped = [], [], [], {}
    for name, (host, description, scopes) in SERVICES.items():
        integration_id = f"google_{name}"
        url = f"https://{host}.googleapis.com/mcp/v1"
        entry = entries.get(integration_id)
        if entry is not None and not _owned_entry(name, entry):
            raise AuthFailure("Existing Google integration differs; it was left untouched")
        if entry is not None and (entry.get("client_id") != client.client_id
                                  or entry.get("redirect_uri") != REDIRECT_URI):
            if not replace:
                raise AuthFailure("Existing Google integration differs; it was left untouched")
            swapped[integration_id] = entry | {"client_id": client.client_id, "redirect_uri": REDIRECT_URI}
        storage = KeychainTokenStorage(url, interactive=True)
        prior = await storage.get_client_info()
        if prior is not None and prior.client_id != client.client_id:
            if not replace:
                raise AuthFailure("Existing Google Keychain client differs; it was left untouched")
            stale.append(storage)
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
    if destination.exists() and destination.read_bytes() != payload and not replace:
        raise AuthFailure(
            "Protected Google credentials already exist; both files were left untouched"
        )
    if destination.exists() and destination.read_bytes() != payload:
        destination.unlink()
    if not destination.exists():
        with os.fdopen(
            os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        ) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    os.chmod(destination, 0o600)
    for storage in stale:
        await storage.clear_tokens()
    for storage in stores:
        await storage.set_client_info(client)
    for key, entry in swapped.items():
        text = f"[integrations.{key}]\n" + "".join(f"{k} = {_toml(v)}\n" for k, v in entry.items()) + "\n"
        existing = re.sub(rf"(?ms)^\[integrations\.{re.escape(key)}\][ \t]*\n.*?(?=^\[|\Z)",
                          lambda match, text=text: text, existing)
    if pending or swapped:
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




# Setup in the app ----------------------------------------------------------------------------------------------------

def ready():
    """Configured and signed in (one token covers every service)."""
    return asyncio.run(_signed_in())


async def _signed_in():
    try:
        entries = tomllib.loads((DATA / "mcp.toml").read_text()).get("integrations", {})
    except (OSError, ValueError):
        return False
    if not all(_owned_entry(name, entries.get(f"google_{name}")) for name in SERVICES):
        return False
    from src import resources

    shipped = resources.google_client()
    try:
        if shipped and _client(shipped).client_id != entries["google_gmail"].get("client_id"):
            return False  # set up with a client this Luna no longer ships: sign in again
    except AuthFailure:
        return False
    try:
        return bool(await KeychainTokenStorage(credential_resource("gmail")).get_tokens())
    except Exception:  # noqa: BLE001  (Keychain unavailable reads as not signed in)
        return False


async def sign_in(report_progress=None):
    """One Google consent screen for every Workspace service's everyday scopes; the token is kept for each service.
    Returns an AuthOutcome."""
    scopes = " ".join(sorted({SCOPE_BASE + s for _, _, service_scopes in SERVICES.values() for s in service_scopes}))
    first = next(iter(SERVICES))
    adapter = GoogleAPIAuthAdapter(first, redirect_uri=REDIRECT_URI, scopes=scopes)

    async def progress(update):
        if report_progress:
            report_progress(update)

    outcome = await adapter.authorize(progress)
    if outcome.status != "connected":
        return outcome
    source = KeychainTokenStorage(credential_resource(first), interactive=True)
    token, expiry = await source.get_tokens(), await source.get_token_expiry()
    for name in SERVICES:
        if name != first:
            storage = KeychainTokenStorage(credential_resource(name), interactive=True)
            remaining = max(0, int((expiry or time.time()) - time.time()))
            await storage.set_tokens(token.model_copy(update={"expires_in": remaining}))
    return outcome


def install(job):
    """The app's Google step: import the bundled client, configure the services, then sign in in the browser."""
    from src import resources
    from src.install import InstallError

    payload = resources.google_client()
    if payload is None:
        raise InstallError("google_client_missing")
    job.report("Preparing Google")
    try:
        asyncio.run(configure(payload, backend="local", replace=True))
    except AuthFailure:
        raise InstallError("google_client_invalid") from None
    job.report("Waiting for you to sign in to Google in your browser")
    try:
        outcome = asyncio.run(asyncio.wait_for(sign_in(), SIGN_IN_SECONDS))
    except TimeoutError:
        raise InstallError("sign_in_timed_out") from None
    if outcome.status != "connected":
        raise InstallError({"denied": "sign_in_cancelled"}.get(outcome.status, outcome.reason or "sign_in_failed"))
