"""The pinned Spotify playback patch is repeatable and preserves local changes."""

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
        override = patch("src.install.spotify.PLAYBACK_PATCH", playback_patch)
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
