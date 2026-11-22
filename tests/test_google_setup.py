"""Google credential import uses temporary files and an in-memory Keychain."""

import copy
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.lowlevel import Server

from src.install import google as setup_google
from src.integrations.auth import AuthFailure, MCPOAuthAdapter
from src.integrations.mcp import MCPRuntime
from src.integrations.workspace.auth import GoogleAPIAuthAdapter
from tests.test_integrations import MemoryStorage


class GoogleSetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "Luna"
        self.data.mkdir()
        self.source = self.root / "download.json"
        self.payload = {
            "installed": {
                "client_id": "test.apps.googleusercontent.com",
                "client_secret": "test-secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        self.source.write_text(json.dumps(self.payload))
        self.config = self.data / "mcp.toml"
        self.config.write_text(
            '[integrations.spotify]\nauth = "spotify"\ncommand = "node"\ncwd = "/tmp/spotify"\n'
        )
        self.records = {}

        def storage(url, *, interactive=False):
            self.assertTrue(interactive)
            return MemoryStorage(
                url, self.records.setdefault(url, {}), interactive=interactive
            )

        for name, value in (("DATA", self.data), ("KeychainTokenStorage", storage)):
            patcher = patch.object(setup_google, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_move_configure_and_preserve_on_rerun(self):
        payload = self.source.read_bytes()
        await setup_google.setup(self.source, backend="hosted")
        destination = self.data / "google-oauth-client.json"
        self.assertFalse(self.source.exists())
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        content = self.config.read_text()
        self.assertNotIn("test-secret", content)
        entries = tomllib.loads(content)["integrations"]
        runtime = MCPRuntime.from_config(self.config)
        self.assertEqual(len(runtime.integrations), 9)
        for name, (host, _, scopes) in setup_google.SERVICES.items():
            integration_id = "google_" + name
            url = f"https://{host}.googleapis.com/mcp/v1"
            self.assertEqual(entries[integration_id]["url"], url)
            self.assertIsInstance(
                runtime.integrations[integration_id].auth, MCPOAuthAdapter
            )
            self.assertEqual(
                entries[integration_id]["redirect_uri"], setup_google.REDIRECT_URI
            )
            self.assertEqual(
                entries[integration_id]["scopes"].split(),
                [setup_google.SCOPE_BASE + s for s in scopes],
            )
            self.assertEqual(
                entries[integration_id]["authorization_params"],
                {"access_type": "offline", "prompt": "consent"},
            )
            self.assertEqual(
                self.records[url]["client_info"]["issuer"], setup_google.ISSUER
            )
            self.assertEqual(
                self.records[url]["client_info"]["client_secret"], "test-secret"
            )
            self.records[url].update(tokens={"refresh_token": "keep"}, expires_at=123)
        snapshot = copy.deepcopy(self.records)
        await setup_google.setup(destination, backend="hosted")
        self.assertEqual(self.records, snapshot)
        self.assertEqual(self.config.read_text(), content)
        self.assertEqual(entries["spotify"]["auth"], "spotify")
        await runtime.close()

    async def test_the_apps_setup_swaps_in_the_client_this_luna_ships_and_drops_old_tokens(self):
        # v0.1-alpha shipped a Web client on /oauth/callback; a later Luna ships a Desktop client.
        await setup_google.setup(self.source, backend="local")
        old = tomllib.loads(self.config.read_text())["integrations"]
        for record in self.records.values():
            record.update(tokens={"access_token": "old", "refresh_token": "old"}, expires_at=123)
        text = self.config.read_text().replace(setup_google.REDIRECT_URI, "http://127.0.0.1:8765/oauth/callback")
        self.config.write_text(text)
        shipped = {"installed": self.payload["installed"] | {"client_id": "new.apps.googleusercontent.com"}}
        payload = json.dumps(shipped).encode()
        with self.assertRaises(AuthFailure):  # the command line never overwrites
            await setup_google.configure(payload)
        self.assertEqual(self.config.read_text(), text)
        with patch("src.resources.google_client", return_value=payload):
            self.assertFalse(await setup_google._signed_in())  # the page offers the sign-in again
            await setup_google.configure(payload, replace=True)
        entries = tomllib.loads(self.config.read_text())["integrations"]
        for name in setup_google.SERVICES:
            entry = entries["google_" + name]
            self.assertEqual((entry["client_id"], entry["redirect_uri"]),
                             ("new.apps.googleusercontent.com", setup_google.REDIRECT_URI))
            self.assertEqual({k: v for k, v in entry.items() if k not in ("client_id", "redirect_uri")},
                             {k: v for k, v in old["google_" + name].items() if k not in ("client_id", "redirect_uri")})
        self.assertEqual(entries["spotify"]["auth"], "spotify")
        self.assertEqual((self.data / "google-oauth-client.json").read_bytes(), payload)
        for record in self.records.values():
            self.assertEqual(record["client_info"]["client_id"], "new.apps.googleusercontent.com")
            self.assertIsNone(record["tokens"])

    async def test_only_a_desktop_app_client_is_imported_and_others_leave_files_untouched(self):
        installed = self.payload["installed"]
        for client in (
            {"installed": installed | {"client_secret": ""}},
            {"installed": installed | {"token_uri": "https://other.example/token"}},
            {"installed": installed | {"client_id": "someone.example.com"}},
            {"web": installed},  # a Web application client's secret must never ship inside the app
        ):
            self.source.write_text(json.dumps(client))
            source, config = self.source.read_bytes(), self.config.read_bytes()
            with self.assertRaises(AuthFailure):
                await setup_google.setup(self.source)
            self.assertEqual(self.source.read_bytes(), source)
            self.assertEqual(self.config.read_bytes(), config)
            self.assertFalse(self.records)
            self.assertFalse((self.data / "google-oauth-client.json").exists())

    async def test_backend_swap_is_reversible_and_preserves_credentials_and_other_integrations(
        self,
    ):
        await setup_google.setup(self.source, backend="hosted")
        original = self.config.read_text()
        records = copy.deepcopy(self.records)
        backup = setup_google.use_backend("local", self.config)
        self.assertEqual(backup.read_text(), original)
        entries = tomllib.loads(self.config.read_text())["integrations"]
        self.assertEqual(
            entries["spotify"], tomllib.loads(original)["integrations"]["spotify"]
        )
        runtime = MCPRuntime.from_config(self.config)
        for name in setup_google.SERVICES:
            key = "google_" + name
            self.assertEqual(entries[key]["backend"], "workspace")
            self.assertNotIn("url", entries[key])
            self.assertNotIn("tools", entries[key])
            self.assertIsInstance(runtime.integrations[key].server, Server)
            self.assertIsInstance(runtime.integrations[key].auth, GoogleAPIAuthAdapter)
        await runtime.close()
        self.assertIsNone(setup_google.use_backend("local", self.config))
        setup_google.use_backend("hosted", self.config)
        self.assertEqual(
            tomllib.loads(self.config.read_text()), tomllib.loads(original)
        )
        self.assertEqual(self.records, records)

    async def test_conflicting_config_and_keychain_leave_files_untouched(self):
        original = self.config.read_text()
        for keychain in (False, True):
            self.config.write_text(
                original
                if keychain
                else original
                + '\n[integrations.google_gmail]\nurl = "https://wrong.example/mcp"\n'
            )
            self.records.clear()
            if keychain:
                url = "https://gmailmcp.googleapis.com/mcp/v1"
                self.records[url] = {"client_info": {"client_id": "other-client"}}
            snapshot = copy.deepcopy(self.records)
            source, config = self.source.read_bytes(), self.config.read_bytes()
            with self.assertRaises(AuthFailure):
                await setup_google.setup(self.source)
            self.assertEqual(self.source.read_bytes(), source)
            self.assertEqual(self.config.read_bytes(), config)
            self.assertEqual(self.records, snapshot)
            self.assertFalse((self.data / "google-oauth-client.json").exists())

    async def test_conflicting_protected_file_preserves_both_credentials(self):
        destination = self.data / "google-oauth-client.json"
        destination.write_text("existing credentials")
        config, source = self.config.read_bytes(), self.source.read_bytes()
        with self.assertRaises(AuthFailure):
            await setup_google.setup(self.source)
        self.assertEqual(destination.read_text(), "existing credentials")
        self.assertEqual(self.source.read_bytes(), source)
        self.assertEqual(self.config.read_bytes(), config)
        self.assertTrue(all(not r for r in self.records.values()))


if __name__ == "__main__":
    unittest.main()
