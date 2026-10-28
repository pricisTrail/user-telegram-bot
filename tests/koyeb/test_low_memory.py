"""Resource-profile tests run without importing Telegram/native dependencies."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "koyeb_profile", ROOT / "deploy" / "koyeb_profile.py"
)
profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile)


def bash_executable() -> str | None:
    if os.name == "nt" and (git := shutil.which("git")):
        candidate = Path(git).parent.parent / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("bash")


class LowMemoryProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.qbit = self.root / "qBittorrent" / "config" / "qBittorrent.conf"
        self.qbit.parent.mkdir(parents=True)
        self.qbit.write_text(
            "[Application]\nMemoryWorkingSetLimit=512\n"
            "[BitTorrent]\nSession\\MaxConnections=-1\n"
            "Session\\AsyncIOThreadsCount=16\n"
            "[Preferences]\nWebUI\\Port=8090\n"
            "WebUI\\LocalHostAuth=false\n",
            encoding="utf-8",
        )
        self.sab = self.root / "sabnzbd" / "SABnzbd.ini"
        self.sab.parent.mkdir()
        self.sab.write_text(
            '__version__ = 19\n[misc]\ncache_limit = ""\n'
            "download_dir = downloads\n[servers]\n[[example]]\n"
            "host = news.example.test\n",
            encoding="utf-8",
        )
        self.config = SimpleNamespace(
            QUEUE_ALL=0, QUEUE_DOWNLOAD=8, QUEUE_UPLOAD=8, BASE_URL_PORT=8080
        )

    def test_disabled_profile_does_not_change_files_or_restored_settings(self):
        originals = (self.qbit.read_bytes(), self.sab.read_bytes())
        aria = {"split": "20"}
        qbit = {"max_connec": -1}
        self.assertFalse(
            profile.apply_low_memory_profile(self.root, self.config, aria, qbit, {})
        )
        self.assertEqual((self.qbit.read_bytes(), self.sab.read_bytes()), originals)
        self.assertEqual(aria, {"split": "20"})
        self.assertEqual(qbit, {"max_connec": -1})
        self.assertEqual(self.config.QUEUE_DOWNLOAD, 8)

    def test_caps_override_restored_settings_and_preserve_unrelated_values(self):
        aria = {
            "split": "20", "disk-cache": "2G",
            "rpc-max-request-size": "1024M", "user-agent": "example-agent",
        }
        qbit = {"max_connec": -1, "web_ui_port": 8090}
        self.assertTrue(profile.apply_low_memory_profile(
            self.root, self.config, aria, qbit, {"MLTB_LOW_MEMORY": "true"}
        ))
        self.assertEqual(
            (self.config.QUEUE_ALL, self.config.QUEUE_DOWNLOAD, self.config.QUEUE_UPLOAD),
            (1, 1, 1),
        )
        self.assertEqual(qbit["max_connec"], 32)
        self.assertEqual(qbit["async_io_threads"], 2)
        self.assertEqual(qbit["web_ui_port"], 8090)
        self.assertEqual(aria["split"], "2")
        self.assertEqual(aria["user-agent"], "example-agent")
        self.assertNotIn("disk-cache", aria)
        self.assertNotIn("rpc-max-request-size", aria)
        qbit_text = self.qbit.read_text(encoding="utf-8")
        self.assertIn("Session\\MaxConnections=32", qbit_text)
        self.assertIn("WebUI\\Port=8090", qbit_text)
        self.assertIn("WebUI\\LocalHostAuth=false", qbit_text)
        sab_text = self.sab.read_text(encoding="utf-8")
        self.assertIn("cache_limit=16M", sab_text)
        self.assertIn("[servers]\n[[example]]\nhost = news.example.test", sab_text)
        self.assertEqual(self.config.BASE_URL_PORT, 8080)

    def test_repeated_application_is_idempotent(self):
        profile.apply_low_memory_profile(
            self.root, self.config, {}, {}, {"MLTB_LOW_MEMORY": "true"}
        )
        originals = (self.qbit.read_bytes(), self.sab.read_bytes())
        profile.apply_low_memory_profile(
            self.root, self.config, {}, {}, {"MLTB_LOW_MEMORY": "true"}
        )
        self.assertEqual((self.qbit.read_bytes(), self.sab.read_bytes()), originals)

    def test_fresh_engine_defaults_are_still_discovered_by_upstream(self):
        aria = {}
        qbit = {}
        profile.apply_low_memory_profile(
            self.root, self.config, aria, qbit, {"MLTB_LOW_MEMORY": "true"}
        )
        self.assertEqual(aria, {})
        self.assertEqual(qbit, {})
        self.assertIn(
            "Session\\MaxConnections=32", self.qbit.read_text(encoding="utf-8")
        )

    def test_mode_is_explicit_and_case_insensitive(self):
        for value in (" TRUE ", "Yes", "1", "on"):
            self.assertTrue(profile.low_memory_enabled({"MLTB_LOW_MEMORY": value}))
        for value in ("", "false", "0", "typo"):
            self.assertFalse(profile.low_memory_enabled({"MLTB_LOW_MEMORY": value}))

    def test_patch_preserves_crlf_and_nested_sections(self):
        self.sab.write_bytes(
            b"[misc]\r\ncache_limit = old\r\n; keep comment\r\n"
            b"[servers]\r\n[[example]]\r\nhost = example.test\r\n"
        )
        profile.patch_ini_section(self.sab, "misc", {"cache_limit": "16M"})
        self.assertEqual(
            self.sab.read_bytes(),
            b"[misc]\r\ncache_limit=16M\r\n; keep comment\r\n"
            b"[servers]\r\n[[example]]\r\nhost = example.test\r\n",
        )

    def test_missing_full_profile_fails_without_creating_partial_config(self):
        self.qbit.unlink()
        with self.assertRaisesRegex(FileNotFoundError, "configuration files"):
            profile.apply_low_memory_profile(
                self.root, self.config, {}, {}, {"MLTB_LOW_MEMORY": "true"}
            )
        self.assertFalse(self.qbit.exists())


@unittest.skipUnless(bash_executable(), "Bash unavailable")
class EngineLauncherTests(unittest.TestCase):
    def launcher_output(self, mode: str, curl_fails: bool = False):
        script = ROOT / "aria-nox-nzb.sh"
        # Stub every external command: no network or download engine is run.
        curl_body = "return 28" if curl_fails else "printf 'https://tracker.example.test/announce\\n'"
        command = (
            "curl() { printf 'CURL:%s\\n' \"$*\" >&2; " + curl_body + "; }; "
            "aria2c() { printf 'ARG:%s\\n' \"$@\"; }; "
            "qbittorrent-nox() { :; }; sabnzbdplus() { :; }; "
            'source "$1"'
        )
        result = subprocess.run(
            [bash_executable(), "-c", command, "launcher-test", script.as_posix()],
            env={**os.environ, "MLTB_LOW_MEMORY": mode},
            text=True, capture_output=True, timeout=10, check=True,
        )
        return (result.stdout + result.stderr).splitlines()

    def test_low_memory_launcher_applies_resource_bounds(self):
        output = self.launcher_output("true")
        for argument in (
            "--disk-cache=8M", "--bt-max-peers=32",
            "--max-concurrent-downloads=1", "--max-connection-per-server=2",
            "--split=2", "--rpc-max-request-size=16M",
            "--optimize-concurrent-downloads=false",
        ):
            self.assertIn("ARG:" + argument, output)

    def test_original_launcher_defaults_remain_available(self):
        output = self.launcher_output("false")
        for argument in (
            "--disk-cache=40M", "--bt-max-peers=0",
            "--max-concurrent-downloads=1000", "--max-connection-per-server=10",
            "--split=10", "--rpc-max-request-size=1024M",
            "--optimize-concurrent-downloads=true",
        ):
            self.assertIn("ARG:" + argument, output)

    def test_launcher_mode_accepts_the_same_whitespace_as_python(self):
        output = self.launcher_output(" TRUE ")
        self.assertIn("ARG:--disk-cache=8M", output)

    def test_tracker_timeout_does_not_prevent_engine_launch(self):
        output = self.launcher_output("true", curl_fails=True)
        self.assertIn("ARG:--max-concurrent-downloads=1", output)
        self.assertIn("ARG:--bt-tracker=[]", output)
        self.assertTrue(any("--connect-timeout 5" in line for line in output))
        self.assertTrue(any("--max-time 15" in line for line in output))


if __name__ == "__main__":
    unittest.main()
