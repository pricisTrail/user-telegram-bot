"""Download guardrails run without Telegram credentials, services, or network."""

import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
from threading import Thread
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "bot/helper/ext_utils/ffmpeg_download.py"
spec = importlib.util.spec_from_file_location("ffmpeg_runner_under_test", RUNNER_PATH)
runner_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner_module
spec.loader.exec_module(runner_module)


def metadata(*, finite=True, rates=True):
    streams = [
        {"index": 0, "codec_type": "video", "height": 1080, "bit_rate": "8000000"},
        {"index": 1, "codec_type": "video", "height": 720, "bit_rate": "3000000"},
        {"index": 2, "codec_type": "video", "height": 480, "bit_rate": "1500000"},
        {"index": 3, "codec_type": "audio", "bit_rate": "128000"},
        {"index": 4, "codec_type": "audio", "bit_rate": "192000"},
    ]
    if not rates:
        for stream in streams:
            stream.pop("bit_rate")
    return {"streams": streams, "format": {"duration": "12.5" if finite else "N/A"}}


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", *, code=0, hangs=False):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        self.returncode = None
        self.code = code
        self.hangs = hangs
        self.killed = False
        self.waited = False
        self.done = asyncio.Event()
        if not hangs:
            self.stdout.feed_eof()
            self.stderr.feed_eof()

    async def wait(self):
        self.waited = True
        if not self.hangs:
            await asyncio.sleep(0)
            if self.returncode is None:
                self.returncode = self.code
            self.done.set()
        await self.done.wait()
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.done.set()


class SelectionTests(unittest.TestCase):
    def test_cover_art_is_not_selected_as_video(self):
        source = metadata()
        source["streams"].append({
            "index": 9, "codec_type": "video", "height": 4000,
            "disposition": {"attached_pic": 1},
        })
        self.assertEqual(runner_module.select_streams(source, 0).stream_indices, (0, 4))
        source["streams"] = [source["streams"][-1], source["streams"][3]]
        selected = runner_module.select_streams(source, 720)
        self.assertEqual(selected.stream_indices, (3,))
        self.assertEqual(selected.height, 0)

    def test_selects_existing_720p_and_best_audio_without_transcoding(self):
        selected = runner_module.select_streams(metadata())
        self.assertEqual(selected.stream_indices, (1, 4))
        self.assertEqual(selected.height, 720)
        self.assertGreater(selected.estimated_size, 0)
        self.assertEqual(runner_module.select_streams(metadata(), 0).stream_indices, (0, 4))

    def test_no_available_lower_resolution_is_an_error(self):
        with self.assertRaisesRegex(runner_module.FFmpegDownloadError, "No video stream"):
            runner_module.select_streams(metadata(), 240)

    def test_audio_only_and_unknown_bitrate_still_work(self):
        selected = runner_module.select_streams({
            "streams": [{"index": 2, "codec_type": "audio", "duration": "25"}],
        })
        self.assertEqual(selected.stream_indices, (2,))
        self.assertEqual(selected.duration, 25)
        self.assertEqual(selected.estimated_size, 0)

    def test_live_and_nonfinite_sources_are_rejected(self):
        for duration in ("N/A", "nan", "inf", 0, -1):
            value = metadata(finite=False)
            value["format"]["duration"] = duration
            with self.subTest(duration=duration), self.assertRaisesRegex(
                runner_module.FFmpegDownloadError, "Live streams"
            ):
                runner_module.select_streams(value)

    def test_url_validation_does_not_expose_input(self):
        for value in ("file:///secret", "https://user:secret@example.org/x", "https://example.org/\nsecret"):
            with self.subTest(value=value), self.assertRaises(runner_module.FFmpegDownloadError) as error:
                runner_module.validate_http_url(value)
            self.assertNotIn("secret", str(error.exception))


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "result.mp4"
        self.calls = []
        self.processes = []
        self.probe_data = json.dumps(metadata()).encode()
        self.ffmpeg_hangs = False
        self.probe_hangs = False
        self.ffmpeg_code = 0
        self.progress_bytes = b"total_size=17\nout_time_us=12500000\nprogress=end\n"
        self.create_output = True
        self.stderr = b""
        self.started = asyncio.Event()
        self.disk = patch.object(runner_module, "disk_usage", return_value=SimpleNamespace(free=4 * 1024**3))
        self.disk.start()
        self.addCleanup(self.disk.stop)
        self.spawn = patch.object(runner_module.asyncio, "create_subprocess_exec", side_effect=self._spawn)
        self.spawn.start()
        self.addCleanup(self.spawn.stop)

    async def _spawn(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "ffprobe":
            process = FakeProcess(self.probe_data, hangs=self.probe_hangs)
        else:
            if self.create_output:
                Path(args[-1]).write_bytes(b"tiny copied media")
            process = FakeProcess(
                self.progress_bytes, self.stderr,
                code=self.ffmpeg_code, hangs=self.ffmpeg_hangs,
            )
            self.started.set()
        self.processes.append(process)
        return process

    async def test_success_uses_explicit_maps_http_whitelist_and_copy(self):
        runner = runner_module.FFmpegDownloadRunner()
        await runner.run("https://example.org/media.m3u8?token=private", self.output)
        probe, command = self.calls
        self.assertEqual(probe[probe.index("-protocol_whitelist") + 1], "http,https,tcp,tls")
        entries = probe[probe.index("-show_entries") + 1].split(":")
        self.assertIn("stream=index,codec_type,height,bit_rate,duration", entries)
        self.assertIn("stream_disposition=attached_pic", entries)
        self.assertIn("format=duration", entries)
        maps = [command[index + 1] for index, item in enumerate(command) if item == "-map"]
        self.assertEqual(maps, ["0:1", "0:4"])
        self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertIn("-xerror", command)
        self.assertNotIn("-fs", command)
        self.assertNotIn("-t", command)
        self.assertEqual(runner.progress_raw, 100)
        self.assertTrue(self.output.is_file())
        self.assertTrue(all(process.waited for process in self.processes))

    async def test_mkv_output_is_honored(self):
        self.output = self.output.with_suffix(".mkv")
        await runner_module.FFmpegDownloadRunner().run("https://example.org/media", self.output)
        self.assertEqual(self.calls[-1][-1], str(self.output))

    async def test_large_probe_output_is_bounded_and_child_is_reaped(self):
        self.probe_data = b"x" * (runner_module.PROBE_STDOUT_LIMIT + 1)
        with self.assertRaisesRegex(runner_module.FFmpegDownloadError, "size limit"):
            await runner_module.FFmpegDownloadRunner().run("https://example.org/media", self.output)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.processes[0].waited)
        self.assertFalse(self.output.exists())

    async def test_probe_timeout_kills_and_waits_for_child(self):
        self.probe_hangs = True
        with self.assertRaisesRegex(runner_module.FFmpegDownloadError, "inspection timed out"):
            await runner_module.FFmpegDownloadRunner(probe_timeout=0.01).run(
                "https://example.org/media", self.output
            )
        self.assertTrue(self.processes[0].killed)
        self.assertTrue(self.processes[0].waited)

    async def test_wall_timeout_removes_partial_file(self):
        self.ffmpeg_hangs = True
        with self.assertRaisesRegex(runner_module.FFmpegDownloadError, "one-hour"):
            await runner_module.FFmpegDownloadRunner(max_seconds=0.01).run(
                "https://example.org/media", self.output
            )
        self.assertTrue(self.processes[-1].killed)
        self.assertFalse(self.output.exists())

    async def test_cancel_kills_and_reaps_process_and_removes_partial_file(self):
        self.ffmpeg_hangs = True
        runner = runner_module.FFmpegDownloadRunner()
        task = asyncio.create_task(runner.run("https://example.org/media", self.output))
        await self.started.wait()
        await runner.cancel()
        with self.assertRaises(runner_module.FFmpegDownloadCancelled):
            await task
        self.assertTrue(self.processes[-1].killed)
        self.assertFalse(self.output.exists())

    async def test_cancellation_while_child_starts_drains_before_wait(self):
        runner = runner_module.FFmpegDownloadRunner()
        process = FakeProcess(b"x" * 200000, b"y" * 200000, hangs=True)

        async def cancelled_spawn(*args, **kwargs):
            runner.cancelled = True
            return process

        with patch.object(runner_module.asyncio, "create_subprocess_exec", side_effect=cancelled_spawn):
            with self.assertRaises(runner_module.FFmpegDownloadCancelled):
                await runner._spawn("ffprobe")
        self.assertTrue(process.killed)
        self.assertTrue(process.stdout.at_eof())
        self.assertTrue(process.stderr.at_eof())

    async def test_estimate_rejects_source_before_ffmpeg_starts(self):
        with patch.object(runner_module, "disk_usage", return_value=SimpleNamespace(free=runner_module.DISK_RESERVE + 10)):
            with self.assertRaisesRegex(runner_module.FFmpegDownloadError, "Insufficient disk"):
                await runner_module.FFmpegDownloadRunner().run("https://example.org/media", self.output)
        self.assertEqual(len(self.calls), 1)

    async def test_actual_size_limit_does_not_upload_truncated_success(self):
        self.probe_data = json.dumps(metadata(rates=False)).encode()
        with patch.object(runner_module, "disk_usage", return_value=SimpleNamespace(free=runner_module.DISK_RESERVE + 5)):
            with self.assertRaisesRegex(runner_module.FFmpegDownloadError, "free disk"):
                await runner_module.FFmpegDownloadRunner().run("https://example.org/media", self.output)
        self.assertFalse(self.output.exists())

    async def test_failed_copy_keeps_stderr_bounded_and_does_not_leak_urls(self):
        self.ffmpeg_code = 1
        self.stderr = b"https://example.org/private?token=secret\n" * 10000
        runner = runner_module.FFmpegDownloadRunner()
        with self.assertRaisesRegex(runner_module.FFmpegDownloadError, "ending in .mkv") as error:
            await runner.run("https://example.org/private?token=secret", self.output)
        self.assertNotIn("secret", str(error.exception))
        self.assertLessEqual(len(runner._stderr_tail), runner_module.STDERR_LIMIT)
        self.assertFalse(self.output.exists())

    async def test_missing_binary_has_an_actionable_safe_error(self):
        with patch.object(runner_module.asyncio, "create_subprocess_exec", side_effect=FileNotFoundError("secret")):
            with self.assertRaisesRegex(runner_module.FFmpegDownloadError, "ffprobe is not installed") as error:
                await runner_module.FFmpegDownloadRunner().run("https://example.org/media", self.output)
        self.assertNotIn("secret", str(error.exception))

    async def test_nonempty_output_is_required_and_existing_output_is_preserved(self):
        self.create_output = False
        with self.assertRaisesRegex(runner_module.FFmpegDownloadError, "nonempty"):
            await runner_module.FFmpegDownloadRunner().run("https://example.org/media", self.output)
        self.output.write_bytes(b"existing user data")
        with self.assertRaisesRegex(runner_module.FFmpegDownloadError, "already exists"):
            await runner_module.FFmpegDownloadRunner().run("https://example.org/media", self.output)
        self.assertEqual(self.output.read_bytes(), b"existing user data")

    async def test_zero_exit_with_early_eof_does_not_accept_partial_media(self):
        value = metadata()
        value["format"]["duration"] = "100"
        self.probe_data = json.dumps(value).encode()
        self.progress_bytes = b"total_size=17\nout_time_us=10000000\nprogress=end\n"
        with self.assertRaisesRegex(runner_module.FFmpegDownloadError, "expected duration"):
            await runner_module.FFmpegDownloadRunner().run("https://example.org/media", self.output)
        self.assertFalse(self.output.exists())

    async def test_zero_exit_with_missing_progress_does_not_accept_partial_media(self):
        self.progress_bytes = b"total_size=17\nprogress=end\n"
        with self.assertRaisesRegex(runner_module.FFmpegDownloadError, "expected duration"):
            await runner_module.FFmpegDownloadRunner().run("https://example.org/media", self.output)
        self.assertFalse(self.output.exists())


class QueueIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.listener = SimpleNamespace(
            name="result.mp4", link="https://example.org/media", mid=123,
            message=object(), multi=0, is_rss=False, is_cancelled=False,
            on_download_start=AsyncMock(), on_download_complete=AsyncMock(),
            on_download_error=AsyncMock(), subproc=None,
        )
        self.tasks = {}
        self.queued = False
        self.event = asyncio.Event()
        self.send_status = AsyncMock()
        self.fake_runner = SimpleNamespace(run=AsyncMock(side_effect=self._write_output), cancel=AsyncMock())
        modules = {}
        for name in (
            "bot", "bot.helper", "bot.helper.ext_utils", "bot.helper.mirror_leech_utils",
            "bot.helper.mirror_leech_utils.download_utils", "bot.helper.mirror_leech_utils.status_utils",
            "bot.helper.telegram_helper",
        ):
            modules[name] = ModuleType(name)
            modules[name].__path__ = []
        modules["bot"].task_dict = self.tasks
        modules["bot"].task_dict_lock = asyncio.Lock()
        modules["bot.helper.ext_utils.ffmpeg_download"] = runner_module
        manager = ModuleType("bot.helper.ext_utils.task_manager")
        manager.check_running_tasks = self._queue
        modules[manager.__name__] = manager
        statuses = ModuleType("bot.helper.mirror_leech_utils.status_utils.ffmpeg_download_status")
        statuses.FFmpegDownloadStatus = lambda listener, downloader, gid: SimpleNamespace(
            listener=listener, downloader=downloader, gid=gid, state="download"
        )
        modules[statuses.__name__] = statuses
        queued = ModuleType("bot.helper.mirror_leech_utils.status_utils.queue_status")
        queued.QueueStatus = lambda listener, gid, state: SimpleNamespace(listener=listener, state="queued")
        modules[queued.__name__] = queued
        messages = ModuleType("bot.helper.telegram_helper.message_utils")
        messages.send_status_message = self.send_status
        modules[messages.__name__] = messages
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        name = "bot.helper.mirror_leech_utils.download_utils.ffmpeg_download"
        adapter_spec = importlib.util.spec_from_file_location(name, ROOT / "bot/helper/mirror_leech_utils/download_utils/ffmpeg_download.py")
        self.adapter = importlib.util.module_from_spec(adapter_spec)
        adapter_spec.loader.exec_module(self.adapter)
        self.runner_patch = patch.object(self.adapter, "FFmpegDownloadRunner", return_value=self.fake_runner)
        self.runner_patch.start()
        self.addCleanup(self.runner_patch.stop)

    async def _queue(self, listener):
        return self.queued, self.event if self.queued else None

    async def _write_output(self, url, path, height):
        Path(path).write_bytes(b"media")

    async def test_normal_task_enters_existing_completion_pipeline(self):
        await self.adapter.add_ffmpeg_download(self.listener, self.directory.name)
        self.listener.on_download_start.assert_awaited_once()
        self.listener.on_download_complete.assert_awaited_once()
        self.listener.on_download_error.assert_not_awaited()
        self.assertEqual(self.listener.size, 5)
        self.assertEqual(self.tasks[123].state, "download")

    async def test_queued_task_waits_then_starts_without_duplicate_start_callback(self):
        self.queued = True
        task = asyncio.create_task(self.adapter.add_ffmpeg_download(self.listener, self.directory.name))
        await asyncio.sleep(0)
        self.assertEqual(self.tasks[123].state, "queued")
        self.fake_runner.run.assert_not_awaited()
        self.event.set()
        await task
        self.listener.on_download_start.assert_awaited_once()
        self.listener.on_download_complete.assert_awaited_once()

    async def test_cancelled_queued_task_never_starts_a_process(self):
        self.queued = True
        task = asyncio.create_task(self.adapter.add_ffmpeg_download(self.listener, self.directory.name))
        await asyncio.sleep(0)
        self.listener.is_cancelled = True
        self.event.set()  # Existing QueueStatus cancellation releases the wait.
        await task
        self.fake_runner.run.assert_not_awaited()
        self.listener.on_download_complete.assert_not_awaited()

    async def test_cancel_and_download_exit_report_one_error(self):
        self.fake_runner.run.side_effect = runner_module.FFmpegDownloadCancelled("cancelled")
        downloader = self.adapter.FFmpegDownloader(self.listener, self.directory.name)
        await asyncio.gather(downloader.cancel_task(), downloader.download())
        self.fake_runner.cancel.assert_awaited_once()
        self.listener.on_download_error.assert_awaited_once()
        self.listener.on_download_complete.assert_not_awaited()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg binaries are unavailable")
class LocalFFmpegIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_tiny_local_dash_is_probed_copied_and_muxed(self):
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture.mpd"
            created = await asyncio.to_thread(subprocess.run, [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=160x90:rate=5",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-threads", "1", "-c:a", "aac", "-b:a", "64000",
                "-f", "dash", str(fixture),
            ], capture_output=True, timeout=20, cwd=directory)
            self.assertEqual(created.returncode, 0, "Unable to create the tiny local DASH test fixture.")
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=directory))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                output = Path(directory) / "copy.mp4"
                runner = runner_module.FFmpegDownloadRunner(max_seconds=20)
                selection = await runner.run(
                    f"http://127.0.0.1:{server.server_port}/fixture.mpd", output
                )
                self.assertEqual(selection.height, 90)
                self.assertEqual(len(selection.stream_indices), 2)
                self.assertGreater(output.stat().st_size, 0)
                self.assertEqual(runner.progress_raw, 100)
                self.assertIsNotNone(runner.process.returncode)
                self.assertEqual(runner.process.returncode, 0)
            finally:
                await asyncio.to_thread(server.shutdown)
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
