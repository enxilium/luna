"""The pinned Spotify playback patch is repeatable and preserves local changes."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.install.spotify import _patch_server


class SpotifySetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "server.txt"
        self.source.write_text("before\n")
        playback_patch = self.directory / "playback.patch"
        playback_patch.write_text(
            "diff --git a/server.txt b/server.txt\n"
            "--- a/server.txt\n+++ b/server.txt\n"
            "@@ -1 +1 @@\n-before\n+after\n"
        )
        override = patch("src.install.spotify.PATCHES", [playback_patch])
        override.start()
        self.addCleanup(override.stop)

    def test_patch_is_idempotent(self):
        _patch_server(self.directory)
        _patch_server(self.directory)
        self.assertEqual(self.source.read_text(), "after\n")

    def test_unrelated_local_changes_survive(self):
        extra = self.directory / "local.txt"
        extra.write_text("keep this\n")
        _patch_server(self.directory)
        self.assertEqual(extra.read_text(), "keep this\n")

    def test_conflicting_local_changes_fail_without_writing(self):
        self.source.write_text("local change\n")
        with self.assertRaises(subprocess.CalledProcessError):
            _patch_server(self.directory)
        self.assertEqual(self.source.read_text(), "local change\n")


class CredentialTests(unittest.TestCase):
    def test_the_spotify_config_never_holds_a_client_secret(self):
        from src.install import spotify

        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "spotify-config.json"
            app = {"clientId": "id", "redirectUri": "http://localhost:8888/callback"}
            with patch.object(spotify, "CONFIG", config), patch.object(spotify.resources, "spotify_app", return_value=app):
                spotify._credentials()
            self.assertEqual(json.loads(config.read_text()),
                             {"clientId": "id", "redirectUri": "http://127.0.0.1:8888/callback"})

    def test_luna_applies_the_pkce_patch_after_the_playback_patch(self):
        from src.install import spotify

        self.assertEqual([p.name for p in spotify.PATCHES], ["spotify-playback.patch", "spotify-pkce.patch"])
        pkce = spotify.PATCHES[1].read_text()
        self.assertIn("code_challenge_method", pkce)
        self.assertNotIn("+  clientSecret", pkce)


class UpgradeTests(unittest.TestCase):
    """A Spotify set up before PKCE: its server is replaced and its secret-flow sign-in has to be redone."""

    def setUp(self):
        from src.install import spotify

        self.spotify = spotify
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.server = Path(folder.name) / "servers/spotify"
        (self.server / "build").mkdir(parents=True)
        (self.server / "build/index.js").write_text("old server")
        self.config = self.server / "spotify-config.json"
        self.config.write_text(json.dumps({"clientId": "id", "clientSecret": "secret",
                                           "redirectUri": "http://127.0.0.1:8888/callback", "refreshToken": "old"}))
        self.prebuilt = Path(folder.name) / "prebuilt"
        (self.prebuilt / "build").mkdir(parents=True)
        (self.prebuilt / "build/index.js").write_text("new server")
        (self.prebuilt / spotify.MARKER).write_text(spotify.patches_digest() + "\n")
        for target, value in (("SERVER", self.server), ("CONFIG", self.config)):
            patcher = patch.object(spotify, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_an_older_server_is_replaced_and_a_secret_flow_sign_in_is_dropped(self):
        app = {"clientId": "id", "redirectUri": "http://127.0.0.1:8888/callback"}
        self.assertFalse(self.spotify.ready())
        with patch.object(self.spotify.resources, "spotify_server", return_value=self.prebuilt), \
                patch.object(self.spotify.resources, "spotify_app", return_value=app):
            self.spotify._server(Path("node"))
            self.assertEqual((self.server / "build/index.js").read_text(), "new server")
            self.assertIn("clientSecret", json.loads(self.config.read_text()))  # the config survived the swap
            self.spotify._credentials()
        self.assertEqual(json.loads(self.config.read_text()), app)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.config.write_text(json.dumps(app | {"refreshToken": "new"}))
        self.assertTrue(self.spotify.ready())

    def test_a_current_server_is_left_alone(self):
        (self.server / self.spotify.MARKER).write_text(self.spotify.patches_digest() + "\n")
        with patch.object(self.spotify.resources, "spotify_server", return_value=self.prebuilt):
            self.spotify._server(Path("node"))
        self.assertEqual((self.server / "build/index.js").read_text(), "old server")
