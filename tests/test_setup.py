"""First-run setup: where the Gemini key comes from, how it's checked and kept, the model download, and voice waiting."""

import asyncio
import os
import queue
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from src import setup
from src.agent.text import TextEvent
from src.ui import app as ui_app


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.env = Path(self.folder.name) / ".env"
        self.env.write_text("GOOGLE_API_KEY=from-dotenv\n")
        self.keychain = {}
        for target, value in (("_keychain_read", self.keychain.get), ("ENV_PATH", self.env), ("DOTENV", True)):
            patcher = patch.object(setup, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_your_own_luna_prefers_the_environment_then_the_keychain_then_dotenv(self):
        with patch.object(setup, "separate_home", return_value=False):
            with patch.dict(os.environ, {"GOOGLE_API_KEY": "from-env"}):
                self.assertEqual(setup.gemini_key(), "from-env")
            with patch.dict(os.environ, {"GOOGLE_API_KEY": ""}):
                self.assertEqual(setup.gemini_key(), "from-dotenv")
                self.keychain["gemini-api-key"] = "from-keychain"
                self.assertEqual(setup.gemini_key(), "from-keychain")

    def test_the_app_never_takes_the_key_from_the_checkouts_dotenv(self):
        # A dev build asks for its key like a new user's Luna would; only scripts and live tests read .env.
        with patch.object(setup, "separate_home", return_value=False), patch.object(setup, "DOTENV", False), \
                patch.dict(os.environ, {"GOOGLE_API_KEY": ""}):
            self.assertEqual(setup.gemini_key(), "")
            self.keychain["gemini-api-key"] = "from-keychain"
            self.assertEqual(setup.gemini_key(), "from-keychain")
        chats = Path(self.folder.name) / "text-chats.sqlite3"  # never your own Luna's lock
        with patch.object(ui_app, "_run_app"), patch.object(ui_app, "CHAT_PATH", chats), patch.object(setup, "DOTENV", True):
            ui_app.run_app(preview=True)
            self.assertFalse(setup.DOTENV)

    def test_a_checkout_and_the_installed_app_keep_separate_keychain_entries(self):
        from src import paths
        from src.dashboard import messages
        from src.integrations.auth import KeychainTokenStorage

        self.assertFalse(paths.PACKAGED)  # the tests run from the checkout
        self.assertEqual((setup.KEYCHAIN_SERVICE, messages.KEYCHAIN_SERVICE), ("Luna (dev)", "Luna Dashboard (dev)"))
        import Security as S
        self.assertEqual(KeychainTokenStorage("https://example.com/mcp")._query()[S.kSecAttrService], "Luna MCP (dev)")
        with patch.object(paths, "PACKAGED", True):
            self.assertEqual(paths.keychain_service("Luna MCP"), "Luna MCP")

    def test_a_separate_home_shares_nothing_with_your_own_luna(self):
        self.keychain["gemini-api-key"] = "yours"
        with patch.object(setup, "separate_home", return_value=True), patch.dict(os.environ, {"GOOGLE_API_KEY": "yours"}):
            self.assertEqual(setup.gemini_key(), "")  # not the environment, your Keychain entry or the checkout's .env
            account = setup._account()
            self.assertTrue(account.startswith("gemini-api-key:"))
            self.keychain[account] = "its own"
            self.assertEqual(setup.gemini_key(), "its own")

    def test_a_key_is_kept_only_after_google_accepts_it(self):
        saved = []
        with patch.object(setup, "_keychain_write", side_effect=lambda account, key: saved.append(key)):
            with patch.object(setup, "check_gemini_key", return_value="invalid_key"):
                self.assertEqual(setup.save_gemini_key("bad"), "invalid_key")
            self.assertEqual(setup.save_gemini_key("   "), "invalid_key")
            with patch.object(setup, "check_gemini_key", return_value="ok") as check:
                self.assertEqual(setup.save_gemini_key("  good  "), "ok")
                check.assert_called_once_with("good")
        self.assertEqual(saved, ["good"])

    def test_the_check_sends_the_key_in_a_header_and_names_what_went_wrong(self):
        def answer(error):
            def urlopen(request, timeout):
                self.assertNotIn("secret", request.full_url)
                self.assertEqual(request.get_header("X-goog-api-key"), "secret")
                if error:
                    raise error
                return open(os.devnull, "rb")
            return urlopen

        cases = ((None, "ok"), (HTTPError("u", 400, "bad", {}, None), "invalid_key"),
                 (HTTPError("u", 403, "denied", {}, None), "invalid_key"), (HTTPError("u", 503, "busy", {}, None), "unreachable"),
                 (URLError("offline"), "unreachable"))
        for error, expected in cases:
            with self.subTest(expected=expected), patch.object(setup, "urlopen", side_effect=answer(error)):
                self.assertEqual(setup.check_gemini_key("secret"), expected)


class DownloadTests(unittest.TestCase):
    def test_one_download_reports_progress_and_ends_done_or_with_a_reason(self):
        for outcome in ("done", "failed"):
            with self.subTest(outcome=outcome):
                changes, finished = [], threading.Event()

                def fetch(progress):
                    progress(5, 10)
                    if outcome == "failed":
                        raise OSError("offline")

                with patch.object(setup, "model_ready", return_value=False), patch.object(setup, "download_model", side_effect=fetch):
                    download = setup.Download()
                    download.on_change = lambda snapshot: (changes.append(snapshot), snapshot["state"] != "running" and finished.set())
                    download.start()
                    download.start()  # already running: no second download
                    self.assertTrue(finished.wait(5))
                self.assertEqual(changes[-1]["state"], outcome)
                self.assertEqual(changes[-1]["error"], None if outcome == "done" else "download_failed")
                self.assertEqual(changes[0]["done"], 5)


class WaitForSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_waits_for_setup_while_chats_explain_then_hands_over(self):
        ready, requests, events = [False], queue.Queue(), []
        with patch("src.setup.voice_ready", side_effect=lambda: ready[0]):
            waiting = asyncio.create_task(ui_app._wait_for_setup(asyncio.Event(), requests, events.append))
            requests.put(type("Request", (), {"chat_id": "c", "message_id": "m"})())
            for _ in range(50):
                if events:
                    break
                await asyncio.sleep(0.05)
            self.assertIsInstance(events[0], TextEvent)
            self.assertIn("isn't set up", events[0].error)
            ready[0] = True
            await asyncio.wait_for(waiting, 5)
        self.assertTrue(requests.empty())  # the stand-in took nothing meant for the real chat agent

    async def test_stopping_during_setup_ends_the_wait(self):
        stopped = asyncio.Event()
        with patch("src.setup.voice_ready", return_value=False):
            waiting = asyncio.create_task(ui_app._wait_for_setup(stopped, None, lambda event: None))
            await asyncio.sleep(0.1)
            stopped.set()
            await asyncio.wait_for(waiting, 5)


if __name__ == "__main__":
    unittest.main()
