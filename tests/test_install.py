"""Installers run from setup: profiles stay apart, jobs report reason codes, downloads report progress, one Google
sign-in covers every service, Canvas keeps its school, and voice restarts when something new is connected."""

import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.shared.auth import OAuthToken

from src import install
from src.install import InstallError, Job, canvas, google, memory
from src.integrations import auth
from src.integrations.auth import AuthOutcome
from src.integrations.workspace import SERVICES, credential_resource
from tests.test_integrations import MemoryStorage

ROOT = Path(__file__).resolve().parents[1]


def paths_for(home):
    env = os.environ | ({"LUNA_HOME": str(home)} if home else {})
    env.pop("LUNA_HOME", None) if not home else None
    code = ("import json, src.paths as p; print(json.dumps([p.PROFILE, p.MEMORY_API_PORT, p.MEMORY_DATABASE_PORT, "
            "p.OLLAMA_PORT, p.MEMORY_LABEL, p.OLLAMA_LABEL, p.MEMORY_DATABASE]))")
    return json.loads(subprocess.check_output([sys.executable, "-c", code], cwd=ROOT, env=env, text=True))


class ProfileTests(unittest.TestCase):
    def test_your_own_luna_keeps_its_services_and_a_separate_home_gets_its_own(self):
        yours = paths_for(None)
        self.assertEqual(yours, ["", 8889, 55439, 11435, "dev.luna.memory", "dev.luna.memory-ollama", "luna-memory"])
        with tempfile.TemporaryDirectory() as folder:
            other = paths_for(folder)
        profile = other[0]
        self.assertEqual(len(profile), 8)
        self.assertNotIn(other[1], (8889,))
        self.assertEqual((other[2] - 55439, other[3] - 11435), (other[1] - 8889,) * 2)
        self.assertEqual(other[4:], [f"dev.luna.memory.{profile}", f"dev.luna.memory-ollama.{profile}", f"luna-memory-{profile}"])

    def test_a_separate_home_never_reads_or_replaces_your_sign_ins(self):
        url = credential_resource("gmail")
        with patch.object(auth, "PROFILE", ""):
            yours = auth.KeychainTokenStorage(url)._account
        self.assertEqual(yours, hashlib.sha256(url.encode()).hexdigest())  # unchanged, so existing grants still apply
        with patch.object(auth, "PROFILE", "abcd1234"):
            self.assertNotEqual(auth.KeychainTokenStorage(url)._account, yours)


    def test_the_dashboards_secrets_follow_the_profile_too(self):
        import Security as S
        from src.dashboard import messages

        with patch("src.paths.PROFILE", ""):
            self.assertEqual(messages._keychain_query("beeper")[S.kSecAttrAccount], "beeper")  # yours, as before
        with patch("src.paths.PROFILE", "abcd1234"):
            self.assertEqual(messages._keychain_query("beeper")[S.kSecAttrAccount], "beeper#abcd1234")


class JobTests(unittest.TestCase):
    def run_job(self, run, **options):
        finished, changes = threading.Event(), []
        job = Job("thing", run, lambda: False)
        job.on_change = lambda name, snapshot: (changes.append(snapshot), snapshot["state"] != "running" and finished.set())
        before = install.generation()
        self.assertTrue(job.start(**options))
        self.assertTrue(finished.wait(5))
        return job, changes, install.generation() - before

    def test_success_counts_as_a_change_and_failures_carry_a_reason_code(self):
        job, changes, bumped = self.run_job(lambda job, value: job.report("Working", done=value, total=2), value=1)
        self.assertEqual((job.state, bumped), ("done", 1))
        self.assertIn({"state": "running", "step": "Working", "done": 0, "total": 0, "error": None}, changes)

        def disk_full(job):
            raise InstallError("disk_full")
        job, changes, bumped = self.run_job(disk_full)
        self.assertEqual((job.state, job.error, bumped), ("failed", "disk_full", 0))

        def broken(job):
            raise KeyError("provider text that must not reach the page")
        job, changes, bumped = self.run_job(broken)
        self.assertEqual((job.state, job.error), ("failed", "install_failed"))


class MemoryTests(unittest.TestCase):
    def test_the_model_download_follows_ollamas_stream(self):
        lines = [b'{"status":"pulling manifest"}\n', b'{"status":"pulling","total":100,"completed":40}\n',
                 b'{"status":"pulling","total":100,"completed":100}\n', b'{"status":"success"}\n']
        seen = []
        with patch.object(memory, "urlopen", return_value=io.BytesIO(b"".join(lines))) as opened:
            memory.pull_model(lambda done, total: seen.append((done, total)))
        self.assertEqual(seen, [(40, 100), (100, 100)])
        self.assertIn(b'"stream": true', opened.call_args.args[0].data)
        with patch.object(memory, "urlopen", return_value=io.BytesIO(b'{"error":"pull model manifest: file does not exist"}\n')):
            with self.assertRaises(InstallError) as failure:
                memory.pull_model(lambda **_: None)
        self.assertEqual(failure.exception.reason, "download_failed")

    def test_a_bundled_model_is_copied_whole_and_a_damaged_one_never_lands(self):
        blob = b"weights"
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        name, tag = memory.MEMORY_MODEL.split(":")
        manifest = Path("manifests/registry.ollama.ai/library") / name / tag
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "app", Path(folder) / "ollama"
            (source / manifest).parent.mkdir(parents=True)
            (source / manifest).write_text(json.dumps({"config": {"digest": digest}, "layers": [{"digest": digest}]}))
            self.assertEqual(memory.model_files(source), [])  # a blob is missing: nothing to copy
            (source / "blobs").mkdir()
            (source / "blobs" / digest.replace(":", "-")).write_bytes(blob)
            seen = []
            memory.copy_model(source, lambda done, total: seen.append((done, total)), target)
            self.assertEqual(memory.model_files(target), memory.model_files(source))
            self.assertEqual(seen[-1], (len(blob), len(blob)))

            (source / "blobs" / digest.replace(":", "-")).write_bytes(b"damaged")
            other = Path(folder) / "other"
            with self.assertRaises(InstallError) as failure:
                memory.copy_model(source, lambda **_: None, other)
            self.assertEqual(failure.exception.reason, "model_damaged")
            self.assertFalse((other / manifest).exists())
            self.assertEqual(list((other / "blobs").iterdir()), [])

    def test_services_carry_a_separate_homes_folder_and_never_write_bytecode(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(memory, "AGENTS", Path(folder)), patch.object(memory, "DATA", Path(folder)), \
                patch.object(memory, "stop_service"), patch.object(memory.subprocess, "run") as run, \
                patch.object(memory, "separate_home", return_value=True):
            memory.service("dev.luna.memory.test", ["/bin/echo"])
            import plistlib
            spec = plistlib.loads((Path(folder) / "dev.luna.memory.test.plist").read_bytes())
        self.assertEqual(spec["EnvironmentVariables"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertIn("LUNA_HOME", spec["EnvironmentVariables"])
        self.assertEqual(run.call_args.args[0][:2], ["launchctl", "bootstrap"])


class GoogleTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_sign_in_is_kept_for_every_workspace_service(self):
        records = {}

        def storage(url, *, interactive=False):
            return MemoryStorage(url, records.setdefault(url, {}), interactive=interactive)

        class Adapter:
            def __init__(self, service, *, redirect_uri, scopes):
                self.scopes = set(scopes.split())
                Adapter.made = self

            async def authorize(self, progress):
                first = storage(credential_resource(next(iter(SERVICES))))
                await first.set_tokens(OAuthToken(access_token="a", token_type="Bearer", refresh_token="r", expires_in=3600))
                return AuthOutcome("connected")

        with patch.object(google, "KeychainTokenStorage", storage), patch.object(google, "GoogleAPIAuthAdapter", Adapter):
            outcome = await google.sign_in()
        self.assertEqual(outcome.status, "connected")
        for name, (_, _, scopes) in SERVICES.items():
            self.assertTrue({google.SCOPE_BASE + s for s in scopes} <= Adapter.made.scopes)
            token = records[credential_resource(name)]["tokens"]
            self.assertEqual((token["access_token"], token["refresh_token"]), ("a", "r"))
            self.assertGreater(records[credential_resource(name)]["expires_at"], 0)


class CanvasTests(unittest.TestCase):
    def test_any_schools_address_is_accepted_and_quercus_is_the_default(self):
        self.assertEqual(canvas.site_address(""), "https://q.utoronto.ca")
        self.assertEqual(canvas.site_address("canvas.ubc.ca"), "https://canvas.ubc.ca")
        self.assertEqual(canvas.site_address("https://q.utoronto.ca/courses/1"), "https://q.utoronto.ca")
        for bad in ("not an address", "http://insecure.example.com", "localhost"):
            self.assertIsNone(canvas.site_address(bad), bad)

    def test_setup_sets_the_school_and_a_refresh_keeps_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mcp.toml"
            canvas.register(path, env=canvas.environment("https://canvas.ubc.ca", "America/Vancouver"))
            self.assertEqual(canvas.register(path), "unchanged")  # a refresh (--update) keeps the school
            env = tomllib.loads(path.read_text())["integrations"]["canvas"]["env"]
            self.assertEqual((env["CANVAS_API_URL"], env["TIMEZONE"]), ("https://canvas.ubc.ca/api/v1", "America/Vancouver"))
            self.assertEqual((env["ALLOWED_WRITE_TOOLS"], env["CANVAS_ROLE"]), ("none", "student"))
            with patch.object(canvas, "DATA", Path(folder)):
                self.assertEqual(canvas.site(), ("https://canvas.ubc.ca", "America/Vancouver"))

    def test_a_rejected_token_is_not_kept(self):
        record = {"token": "old"}
        storage = MemoryStorage("api-token:canvas", record)
        adapter = auth.APITokenAuthAdapter("api-token:canvas", "CANVAS_API_TOKEN", storage)
        job = Job("canvas", None, lambda: False)
        with patch.object(canvas, "adapter", return_value=adapter), patch.object(canvas, "install_server"), \
                patch.object(canvas, "test", return_value=False), patch.object(canvas, "register") as register:
            with self.assertRaises(InstallError) as failure:
                canvas.install(job, "q.utoronto.ca", "new")
        self.assertEqual((failure.exception.reason, record), ("token_rejected", {"token": "old"}))
        register.assert_not_called()

    def test_setup_may_ask_the_keychain_for_a_token_an_earlier_luna_saved(self):
        # An item another copy of Luna wrote needs the user's OK: setup must allow the prompt, not fail silently.
        storage = MemoryStorage("api-token:canvas", {"token": "old"})
        reads = []
        storage._read = lambda: reads.append(storage._interactive.get()) or {"token": "old"}
        adapter = auth.APITokenAuthAdapter("api-token:canvas", "CANVAS_API_TOKEN", storage)
        with patch.object(canvas, "adapter", return_value=adapter), patch.object(canvas, "install_server"), \
                patch.object(canvas, "test", return_value=True), patch.object(canvas, "register"):
            canvas.install(Job("canvas", None, lambda: False), "q.utoronto.ca", "new")
        self.assertEqual((reads, storage.record), ([True], {"token": "new"}))

    def test_a_keychain_refusal_is_reported_as_one(self):
        def refused(job):
            raise auth.AuthRequired("keychain_access_required")

        job = Job("canvas", refused, lambda: False)
        job._thread()
        self.assertEqual((job.state, job.error), ("failed", "keychain_access_required"))
        job = Job("canvas", lambda job: (_ for _ in ()).throw(OSError("provider text")), lambda: False)
        job._thread()
        self.assertEqual(job.error, "install_failed")  # anything else stays generic


class VoiceRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_restarts_with_what_setup_just_connected(self):
        from src.ui import app as ui_app

        runs, stopped = [], asyncio.Event()

        async def conversation(*, stopped, **_):
            runs.append(stopped)
            await stopped.wait()

        with patch("src.agent.conversation.run_conversation", conversation):
            voice = asyncio.create_task(ui_app._run_voice(stopped, None, type("Events", (), {"put": print})()))
            await asyncio.sleep(0.1)
            install.changed()  # e.g. Google just connected
            for _ in range(40):
                if len(runs) == 2:
                    break
                await asyncio.sleep(0.1)
            self.assertEqual(len(runs), 2)
            self.assertTrue(runs[0].is_set())
            stopped.set()
            await asyncio.wait_for(voice, 5)
        self.assertEqual(len(runs), 2)


if __name__ == "__main__":
    unittest.main()
