"""Exercise native-fragment orchestration without Telegram or remote media."""

import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "ffl_parallel_test_modules"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "bot/helper/ext_utils")]
sys.modules[PACKAGE] = package


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


base = load_module(f"{PACKAGE}.ffmpeg_download", ROOT / "bot/helper/ext_utils/ffmpeg_download.py")
parallel = load_module(f"{PACKAGE}.ffmpeg_parallel", ROOT / "bot/helper/ext_utils/ffmpeg_parallel.py")


def metadata(duration=10, height=1080, *, audio=True):
    streams = [{
        "index": 0, "codec_type": "video", "width": height * 16 // 9,
        "height": height, "bit_rate": "40", "duration": str(duration),
    }]
    if audio:
        streams.append({"index": 1, "codec_type": "audio", "bit_rate": "8", "duration": str(duration)})
    return {"streams": streams, "format": {"duration": str(duration)}}


class FakeStdin:
    def __init__(self, process):
        self.process = process
        self.pending = bytearray()
        self.error = None
        self.closed = False

    def write(self, data):
        self.pending.extend(data)

    def close(self):
        self.closed = True

    async def drain(self):
        if self.error is not None:
            raise self.error
        while b"\n" in self.pending:
            line, _, remaining = self.pending.partition(b"\n")
            self.pending[:] = remaining
            await self.process.input_line(bytes(line))


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", *, code=0, worker=None):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeStdin(self)
        self.returncode = None
        self.code = code
        self.worker = worker
        self.done = asyncio.Event()
        self.waited = False
        self.killed = False
        self.request = None
        self.acknowledged = False
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        if worker is None:
            self.finish(code)

    def finish(self, code=0):
        self.returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.done.set()

    def event(self, value):
        self.stdout.feed_data(json.dumps(value).encode() + b"\n")

    async def input_line(self, line):
        await self.worker(self, line)

    async def wait(self):
        self.waited = True
        await self.done.wait()
        return self.returncode

    def kill(self):
        self.killed = True
        self.finish(-9)


class ParallelRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "result.mp4"
        self.calls = []
        self.processes = []
        self.worker_process = None
        self.native_mode = "success"
        self.worker_started = asyncio.Event()
        self.worker_acknowledged = asyncio.Event()
        self.initial_metadata = metadata()
        self.output_metadata = metadata()
        self.plan = {
            "event": "plan", "duration": 10, "estimated_size": 1000,
            "tracks": [
                {"file": "track0.bin", "kind": "video", "width": 1920, "height": 1080, "estimated_size": 900},
                {"file": "track1.bin", "kind": "audio", "estimated_size": 100},
            ],
        }
        self.track_bytes = 10
        self.merged_bytes = 20
        self.free = base.DISK_RESERVE + 1024**3
        self.patches = [
            patch.object(base.asyncio, "create_subprocess_exec", side_effect=self.spawn),
            patch.object(base, "disk_usage", side_effect=lambda path: SimpleNamespace(free=self.free)),
            patch.object(parallel, "disk_usage", side_effect=lambda path: SimpleNamespace(free=self.free)),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def worker_input(self, process, line):
        if process.request is None:
            process.request = json.loads(line)
            self.worker_started.set()
            if self.native_mode == "unsupported":
                process.event({"event": "unsupported"})
                process.finish()
            elif self.native_mode == "malformed":
                process.stdout.feed_data(b"SYNTHETIC_PRIVATE_URL_DATA\n")
                process.finish()
            elif self.native_mode == "early_progress":
                process.event({"event": "progress", "track": 0, "downloaded_bytes": 1})
                process.finish()
            else:
                process.event(self.plan)
            return
        if line != b"go":
            raise AssertionError("The parent must acknowledge a validated plan before download.")
        process.acknowledged = True
        workdir = Path(process.request["workdir"])
        for index in range(len(self.plan["tracks"])):
            (workdir / f"track{index}.bin").write_bytes(b"x" * self.track_bytes)
            process.event({
                "event": "progress", "track": index, "downloaded_bytes": self.track_bytes,
                "total_bytes": self.track_bytes, "fragment_index": 2, "fragment_count": 2,
                "status": "finished",
            })
        self.worker_acknowledged.set()
        if self.native_mode == "hang":
            return
        if self.native_mode == "failure":
            process.stderr.feed_data(b"SYNTHETIC_PRIVATE_URL_DATA" * 10000)
            process.event({"event": "error"})
            process.finish(1)
            return
        process.event({"event": "complete"})
        process.finish()

    async def spawn(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "ffprobe":
            value = self.initial_metadata if str(args[-1]).startswith("https:") else self.output_metadata
            process = FakeProcess(json.dumps(value).encode())
        elif "deploy.ffmpeg_fragment_worker" in args:
            process = FakeProcess(worker=self.worker_input)
            self.worker_process = process
            if self.native_mode == "pipe_failure":
                process.stdin.error = BrokenPipeError("SYNTHETIC_PRIVATE_URL_DATA")
                process.stdout.feed_data(b"unowned stdout data" * 10000)
                process.stderr.feed_data(b"unowned stderr data" * 10000)
        else:
            Path(args[-1]).write_bytes(b"y" * self.merged_bytes)
            process = FakeProcess(b"out_time_us=10000000\ntotal_size=20\nprogress=end\n")
        self.processes.append(process)
        return process

    def assert_cleaned(self, *, output_exists=False):
        self.assertEqual(self.output.exists(), output_exists)
        self.assertEqual(list(self.output.parent.iterdir()), [self.output] if output_exists else [])
        self.assertTrue(all(process.waited for process in self.processes))

    async def run_download(self, **options):
        runner = parallel.ParallelFFmpegDownloadRunner(**options)
        result = await runner.run("https://example.org/manifest.mpd?signature=synthetic", self.output, 1080)
        return runner, result

    async def test_1080_download_acknowledges_plan_then_merges_local_tracks(self):
        runner, result = await self.run_download()

        self.assertTrue(self.worker_process.acknowledged)
        self.assertTrue(self.worker_process.stdin.closed)
        self.assertEqual(self.worker_process.request["workers"], 2)
        self.assertEqual(self.worker_process.request["max_height"], 1080)
        self.assertEqual((result.width, result.height, result.duration), (1920, 1080, 10))
        self.assertEqual(runner.progress_raw, 100)
        merge = next(call for call in self.calls if call[0] == "ffmpeg")
        inputs = [merge[index + 1] for index, value in enumerate(merge) if value == "-i"]
        self.assertEqual(len(inputs), 2)
        self.assertEqual([Path(value).name for value in inputs], ["track0.bin", "track1.bin"])
        self.assertFalse(any("https:" in value for value in inputs))
        self.assertEqual(merge[merge.index("-c") + 1], "copy")
        self.assertNotIn("signature", " ".join(next(call for call in self.calls if "deploy.ffmpeg_fragment_worker" in call)))
        self.assert_cleaned(output_exists=True)

    async def test_unsupported_native_source_falls_back_before_acknowledgement(self):
        self.native_mode = "unsupported"

        await self.run_download()

        self.assertFalse(self.worker_process.acknowledged)
        merge = next(call for call in self.calls if call[0] == "ffmpeg")
        self.assertTrue(merge[merge.index("-i") + 1].startswith("https:"))
        self.assert_cleaned(output_exists=True)

    async def test_non_dash_source_uses_existing_copy_pipeline(self):
        runner = parallel.ParallelFFmpegDownloadRunner()
        await runner.run("https://example.org/video.mp4", self.output, 1080)
        self.assertIsNone(self.worker_process)
        self.assert_cleaned(output_exists=True)

    async def test_track_and_merge_storage_is_checked_before_go(self):
        self.free = base.DISK_RESERVE + 1500

        with self.assertRaisesRegex(base.FFmpegDownloadError, "tracks and merged file"):
            await self.run_download()

        self.assertFalse(self.worker_process.acknowledged)
        self.assertTrue(self.worker_process.killed)
        self.assert_cleaned()

    async def test_disk_guard_counts_track_files_and_merged_output_together(self):
        self.free = base.DISK_RESERVE + 1000
        self.plan["estimated_size"] = 0
        self.track_bytes = 300
        self.merged_bytes = 500

        with self.assertRaisesRegex(base.FFmpegDownloadError, "free disk"):
            await self.run_download()

        self.assertTrue(self.worker_process.acknowledged)
        self.assert_cleaned()

    async def test_native_failure_is_sanitized_and_removes_partial_tracks(self):
        self.native_mode = "failure"
        with self.assertRaises(base.FFmpegDownloadError) as captured:
            await self.run_download()
        self.assertNotIn("SYNTHETIC_PRIVATE_URL_DATA", str(captured.exception))
        self.assert_cleaned()

    async def test_invalid_worker_output_is_sanitized(self):
        self.native_mode = "malformed"
        with self.assertRaises(base.FFmpegDownloadError) as captured:
            await self.run_download()
        self.assertNotIn("SYNTHETIC_PRIVATE_URL_DATA", str(captured.exception))
        self.assertFalse(self.worker_process.acknowledged)
        self.assert_cleaned()

    async def test_initial_request_pipe_failure_kills_drains_and_reaps_worker(self):
        self.native_mode = "pipe_failure"
        with self.assertRaisesRegex(base.FFmpegDownloadError, "stopped unexpectedly") as captured:
            await self.run_download()
        self.assertNotIn("SYNTHETIC_PRIVATE_URL_DATA", str(captured.exception))
        self.assertIsNone(self.worker_process.request)
        self.assertFalse(self.worker_process.acknowledged)
        self.assertTrue(self.worker_process.killed)
        self.assertTrue(self.worker_process.stdout.at_eof())
        self.assertTrue(self.worker_process.stderr.at_eof())
        self.assertTrue(self.worker_process.stdin.closed)
        self.assert_cleaned()

    async def test_progress_before_validated_plan_is_rejected(self):
        self.native_mode = "early_progress"
        with self.assertRaises(base.FFmpegDownloadError):
            await self.run_download()
        self.assertFalse(self.worker_process.acknowledged)
        self.assert_cleaned()

    async def test_cancel_kills_worker_and_cleans_its_partial_files(self):
        self.native_mode = "hang"
        runner = parallel.ParallelFFmpegDownloadRunner()
        task = asyncio.create_task(runner.run("https://example.org/media.mpd", self.output, 1080))
        await self.worker_started.wait()
        await asyncio.sleep(0)
        await runner.cancel()
        with self.assertRaises(base.FFmpegDownloadCancelled):
            await task
        self.assertTrue(self.worker_process.killed)
        self.assert_cleaned()

    async def test_cancel_preserves_cancel_error_when_workspace_was_already_removed(self):
        self.native_mode = "hang"
        runner = parallel.ParallelFFmpegDownloadRunner()
        task = asyncio.create_task(runner.run("https://example.org/media.mpd", self.output, 1080))
        await self.worker_acknowledged.wait()
        workspace = Path(self.worker_process.request["workdir"]).resolve()
        self.assertEqual(workspace.parent, self.output.parent.resolve())
        self.assertTrue(workspace.name.startswith(".ffl-fragments-"))
        shutil.rmtree(workspace)
        await runner.cancel()
        with self.assertRaises(base.FFmpegDownloadCancelled):
            await task
        self.assertTrue(self.worker_process.stdin.closed)
        self.assert_cleaned()

    async def test_whole_job_deadline_kills_worker_and_removes_partial_files(self):
        self.native_mode = "hang"
        original_wait_for = asyncio.wait_for
        outer_deadline = 17

        async def start_deadline_after_partial_download(awaitable, timeout):
            if timeout != outer_deadline:
                return await original_wait_for(awaitable, timeout)
            running = asyncio.ensure_future(awaitable)
            try:
                # Start the short test deadline once the intended phase exists;
                # slow test discovery/imports must not time out the source probe.
                await original_wait_for(self.worker_acknowledged.wait(), 2)
                return await original_wait_for(running, 0.02)
            finally:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)

        with patch.object(base.asyncio, "wait_for", side_effect=start_deadline_after_partial_download):
            with self.assertRaisesRegex(base.FFmpegDownloadError, "one-hour"):
                await self.run_download(max_seconds=outer_deadline)
        self.assertTrue(self.worker_acknowledged.is_set())
        self.assertTrue(self.worker_process.killed)
        self.assert_cleaned()

    async def test_merged_output_with_early_eof_is_rejected(self):
        self.output_metadata = metadata(duration=3)
        with self.assertRaisesRegex(base.FFmpegDownloadError, "expected duration"):
            await self.run_download()
        self.assert_cleaned()

    async def test_short_video_track_cannot_hide_behind_full_container_duration(self):
        self.output_metadata["streams"][0]["duration"] = "3"
        with self.assertRaisesRegex(base.FFmpegDownloadError, "media track ended before its expected duration"):
            await self.run_download()
        self.assert_cleaned()

    async def test_merged_output_missing_expected_audio_is_rejected(self):
        self.output_metadata = metadata(audio=False)
        with self.assertRaisesRegex(base.FFmpegDownloadError, "expected audio"):
            await self.run_download()
        self.assert_cleaned()

    async def test_merged_output_missing_expected_video_is_rejected(self):
        self.output_metadata = {
            "streams": [{"index": 0, "codec_type": "audio", "duration": "10"}],
            "format": {"duration": "10"},
        }
        with self.assertRaises(base.FFmpegDownloadError):
            await self.run_download()
        self.assert_cleaned()

    async def test_merged_output_above_requested_quality_is_rejected(self):
        self.output_metadata = metadata(height=2160)
        with self.assertRaises(base.FFmpegDownloadError):
            await self.run_download()
        self.assert_cleaned()

    async def test_unsafe_track_path_is_rejected_before_go(self):
        self.plan["tracks"][0]["file"] = "../unrelated.media"
        with self.assertRaises(base.FFmpegDownloadError):
            await self.run_download()
        self.assertFalse(self.worker_process.acknowledged)
        self.assert_cleaned()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe") and importlib.util.find_spec("yt_dlp"), "FFmpeg and yt-dlp are required")
class NativeDASHIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_dash_native_fragments_merge_into_complete_video(self):
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture.mpd"
            created = await asyncio.to_thread(subprocess.run, [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=160x90:rate=5",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-threads", "1", "-g", "5", "-c:a", "aac", "-b:a", "64000",
                "-seg_duration", "1", "-f", "dash", str(fixture),
            ], capture_output=True, timeout=20, cwd=directory)
            self.assertEqual(created.returncode, 0, "Unable to create the tiny local DASH fixture.")
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=directory))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                output = Path(directory) / "copy.mp4"
                runner = parallel.ParallelFFmpegDownloadRunner(max_seconds=30)
                selection = await runner.run(f"http://127.0.0.1:{server.server_port}/fixture.mpd", output)
                self.assertIsNotNone(runner._fragment_plan, "The integration must exercise native fragments rather than fallback.")
                self.assertTrue(runner._fragment_complete)
                self.assertEqual((selection.width, selection.height), (160, 90))
                self.assertGreaterEqual(selection.duration, 2.9)
                self.assertEqual(runner.progress_raw, 100)
                self.assertGreater(output.stat().st_size, 0)
                self.assertFalse(any(Path(directory).glob(".ffl-fragments-*")))
            finally:
                await asyncio.to_thread(server.shutdown)
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
