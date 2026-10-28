"""Native fragment worker checks run without Telegram or real credentials."""

from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Lock, Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "fragment_worker_under_test", ROOT / "deploy/ffmpeg_fragment_worker.py"
)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def metadata():
    def fmt(kind, height, bitrate):
        return {
            "format_id": f"private-{kind}-{height}",
            "protocol": "http_dash_segments", "url": "https://example.org/private.mpd?secret=marker",
            "fragment_base_url": "https://example.org/segments/",
            "fragments": [{"url": "init.m4s"}, {"url": "first.m4s"}],
            "vcodec": "h264" if kind == "video" else "none",
            "acodec": "none" if kind == "video" else "aac",
            "height": height, "width": int(height * 16 / 9), "tbr": bitrate,
        }
    return {
        "duration": 120,
        "formats": [fmt("video", 1080, 8000), fmt("video", 720, 3000), fmt("audio", 0, 128)],
        "http_headers": {"X-Synthetic-Secret": "DO_NOT_PRINT"},
    }


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = io.StringIO()
        self.raw = metadata()
        self.options = None
        self.downloads = []
        self.download_success = True
        self.extract_failure = None
        test = self

        class Ydl:
            def __init__(self, options):
                test.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, url, **kwargs):
                test.assertEqual(kwargs, {"download": False, "process": False})
                print("SDK stdout URL https://example.org/private?secret=marker")
                print("SDK stderr DO_NOT_PRINT", file=sys.stderr)
                if test.extract_failure:
                    raise test.extract_failure
                return deepcopy(test.raw)

        class Downloader:
            def __init__(self, ydl, options):
                self.hooks = []
                test.assertIs(options, test.options)

            def add_progress_hook(self, hook):
                self.hooks.append(hook)

            def download(self, name, info):
                test.downloads.append((name, info))
                for hook in self.hooks:
                    hook({"status": "downloading", "downloaded_bytes": 5,
                          "total_bytes": 10, "url": "private", "filename": "DO_NOT_PRINT"})
                Path(name).write_bytes(b"sample-media")
                for hook in self.hooks:
                    hook({"status": "finished", "downloaded_bytes": 12, "total_bytes": 12})
                return test.download_success, True

        self.sdk = (Ydl, Downloader)

    def request(self, **updates):
        value = {
            "url": "https://example.org/private.mpd?secret=marker", "workdir": self.directory.name,
            "max_height": 720, "duration": 120, "workers": 2,
        }
        value.update(updates)
        return value

    def run_worker(self, *, ack="go\n", request=None):
        content = json.dumps(self.request() if request is None else request) + "\n" + ack
        result = worker.main(io.StringIO(content), self.output, self.sdk)
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        return result, events

    def test_native_tracks_are_sequential_and_fixed_output_names_are_used(self):
        result, events = self.run_worker()

        self.assertEqual(result, 0)
        self.assertEqual([Path(name).name for name, _ in self.downloads], ["track0.bin", "track1.bin"])
        self.assertEqual([info["height"] for _, info in self.downloads], [720, 0])
        self.assertEqual(events[0]["event"], "plan")
        self.assertEqual([track["kind"] for track in events[0]["tracks"]], ["video", "audio"])
        self.assertEqual(events[0]["width"], 1280)
        self.assertEqual(events[0]["height"], 720)
        self.assertEqual(events[-1], {"event": "complete"})
        self.assertGreater(events[0]["estimated_size"], 0)
        for _, info in self.downloads:
            self.assertFalse(info["is_live"])
            self.assertNotIn("requested_formats", info)
        self.assertEqual(self.options["concurrent_fragment_downloads"], 2)
        self.assertEqual(self.options["external_downloader"], "native")
        self.assertEqual(self.options["fixup"], "never")
        self.assertFalse(self.options["skip_unavailable_fragments"])
        self.assertEqual(self.options["postprocessors"], [])
        self.assertEqual(self.options["allowed_extractors"], ["generic"])
        self.assertFalse(self.options["enable_file_urls"])
        self.assertFalse(self.options["allow_unplayable_formats"])
        self.assertFalse(self.options["usenetrc"])
        self.assertIsNone(self.options["cookiefile"])
        self.assertNotIn("secret", self.output.getvalue())
        self.assertNotIn("DO_NOT_PRINT", self.output.getvalue())
        self.assertNotIn("private-video", self.output.getvalue())
        self.assertNotIn(self.directory.name, self.output.getvalue())

    def test_explicit_1080_and_best_select_the_highest_existing_representation(self):
        for height in (1080, 0):
            with self.subTest(height=height):
                plan, tracks = worker.build_plan(metadata(), height, 120)
                self.assertEqual(plan["height"], 1080)
                self.assertEqual(plan["width"], 1920)
                self.assertEqual(tracks[0]["height"], 1080)

    def test_unsupported_second_track_is_detected_before_any_media_download(self):
        self.raw["formats"][-1]["fragments"][-1]["url"] = "file:///private"
        result, events = self.run_worker()
        self.assertEqual(result, 0)
        self.assertEqual(events, [{"event": "unsupported"}])
        self.assertEqual(self.downloads, [])

    def test_generic_dash_relative_fragment_paths_are_validated(self):
        raw = metadata()
        for fmt in raw["formats"]:
            fmt["fragments"] = [{"path": "init.m4s"}, {"path": "first.m4s"}]
        plan, _ = worker.build_plan(raw, 720, 120)
        self.assertEqual(plan["height"], 720)
        raw["formats"][-1]["fragments"][1]["path"] = "file:///private"
        with self.assertRaises(worker.UnsupportedMedia):
            worker.build_plan(raw, 720, 120)

    def test_drm_live_external_protocol_and_unbounded_fragments_fall_back(self):
        alterations = (
            lambda raw: raw.update({"has_drm": True}),
            lambda raw: raw.update({"is_live": True}),
            lambda raw: raw["formats"][1].update({"protocol": "m3u8_native"}),
            lambda raw: raw["formats"][1].update({"fragments": []}),
            lambda raw: raw["formats"][1].update({"fragments": [{"url": "x"}] * (worker.MAX_FRAGMENTS + 1)}),
        )
        for alter in alterations:
            raw = metadata()
            alter(raw)
            with self.subTest(alter=alter), self.assertRaises(worker.UnsupportedMedia):
                worker.build_plan(raw, 720, 120)

    def test_missing_duration_uses_parent_probe_but_conflicting_duration_falls_back(self):
        raw = metadata()
        del raw["duration"]
        self.assertEqual(worker.build_plan(raw, 720, 120)[0]["duration"], 120)
        raw["duration"] = 150
        with self.assertRaises(worker.UnsupportedMedia):
            worker.build_plan(raw, 720, 120)

    def test_parent_must_ack_plan_before_media_bytes_are_downloaded(self):
        result, events = self.run_worker(ack="")
        self.assertEqual(result, 1)
        self.assertEqual([event["event"] for event in events], ["plan", "error"])
        self.assertEqual(self.downloads, [])

    def test_native_failure_and_sdk_exception_are_generic_and_do_not_leak(self):
        self.extract_failure = RuntimeError("https://example.org/private?secret=DO_NOT_PRINT")
        result, events = self.run_worker()
        self.assertEqual(result, 1)
        self.assertEqual(events, [{"event": "error"}])
        self.extract_failure = None
        self.output = io.StringIO()
        self.download_success = False
        result, events = self.run_worker()
        self.assertEqual(result, 1)
        self.assertEqual(events[-1], {"event": "error"})
        self.assertNotIn("DO_NOT_PRINT", self.output.getvalue())

    def test_existing_file_is_not_overwritten(self):
        path = Path(self.directory.name) / "track0.bin"
        path.write_bytes(b"keep")
        result, events = self.run_worker()
        self.assertEqual(result, 1)
        self.assertEqual(events, [{"event": "error"}])
        self.assertEqual(path.read_bytes(), b"keep")
        self.assertEqual(self.downloads, [])

    def test_oversize_and_invalid_requests_only_emit_generic_error(self):
        for request in ("x" * (worker.MAX_REQUEST + 1), "[]\n", "{invalid secret}\n"):
            output = io.StringIO()
            self.assertEqual(worker.main(io.StringIO(request), output, self.sdk), 1)
            self.assertEqual(output.getvalue(), '{"event":"error"}\n')

    def test_source_credentials_and_non_http_fragments_are_not_downloaded(self):
        for value in ("file:///private.mpd", "https://user:private@example.org/file.mpd", "https://example.org/x\n.mpd"):
            with self.subTest(value=value):
                self.output = io.StringIO()
                result, events = self.run_worker(request=self.request(url=value))
                self.assertEqual(result, 0)
                self.assertEqual(events, [{"event": "unsupported"}])
                self.assertEqual(self.downloads, [])

    def test_progress_is_throttled_and_contains_only_safe_numeric_fields(self):
        emitter = worker.Emitter(self.output)
        hook = emitter.hook(0)
        with patch.object(worker, "monotonic", side_effect=[1, 1.1, 1.4, 1.6, 1.61]):
            for index in range(4):
                hook({"status": "downloading", "downloaded_bytes": index,
                      "total_bytes": 20, "fragment_index": index, "fragment_count": 4,
                      "url": "DO_NOT_PRINT"})
            hook({"status": "finished", "downloaded_bytes": 20, "total_bytes": 20})
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(len(events), 3)
        self.assertEqual(events[1]["fragment_index"], 3)
        self.assertEqual(events[1]["fragment_count"], 4)
        self.assertEqual(events[-1]["status"], "finished")
        self.assertNotIn("DO_NOT_PRINT", self.output.getvalue())

    def test_large_fragment_is_read_with_a_strict_memory_bound(self):
        class ReadTracking(io.BytesIO):
            def read(self, size=-1):
                self.last_read_size = size
                return super().read(size)

        stream = ReadTracking(b"x" * 65)
        downloader = SimpleNamespace(sanitize_open=lambda name, mode: (stream, name))
        with patch.object(worker, "MAX_FRAGMENT_BYTES", 64), self.assertRaises(RuntimeError):
            worker.read_fragment_bounded(downloader, {"fragment_filename_sanitized": "synthetic-fragment"})
        self.assertEqual(stream.last_read_size, 65)
        self.assertTrue(stream.closed)

    def test_small_fragment_is_returned_and_input_is_closed(self):
        stream = io.BytesIO(b"media")
        downloader = SimpleNamespace(sanitize_open=lambda name, mode: (stream, name))
        context = {"fragment_filename_sanitized": "synthetic-fragment"}
        self.assertEqual(worker.read_fragment_bounded(downloader, context), b"media")
        self.assertTrue(stream.closed)


@unittest.skipUnless(
    importlib.util.find_spec("yt_dlp") and shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "yt-dlp and FFmpeg binaries are required",
)
class LocalNativeDashTests(unittest.TestCase):
    def test_native_dash_downloads_two_fragments_concurrently_without_subprocesses(self):
        state = SimpleNamespace(active=0, peak=0, lock=Lock())

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fragment = self.path.endswith(".m4s")
                if fragment:
                    with state.lock:
                        state.active += 1
                        state.peak = max(state.peak, state.active)
                    time.sleep(0.08)
                try:
                    super().do_GET()
                finally:
                    if fragment:
                        with state.lock:
                            state.active -= 1

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture.mpd"
            created = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "5",
                "-threads", "1", "-c:a", "aac", "-b:a", "64000", "-seg_duration", "0.5",
                "-f", "dash", str(fixture),
            ], capture_output=True, timeout=20, cwd=directory)
            self.assertEqual(created.returncode, 0, "Unable to create synthetic DASH fixture")
            workdir = Path(directory) / "tracks"
            workdir.mkdir()
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=directory))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = {
                    "url": f"http://127.0.0.1:{server.server_port}/fixture.mpd",
                    "workdir": str(workdir), "max_height": 720, "duration": 2, "workers": 2,
                }
                output = io.StringIO()
                # Any automatic external FFmpeg/downloader fallback is a test failure.
                with patch("subprocess.Popen", side_effect=AssertionError("Unexpected child process")):
                    result = worker.main(io.StringIO(json.dumps(request) + "\ngo\n"), output)
                self.assertEqual(result, 0, output.getvalue())
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(events[-1], {"event": "complete"})
                self.assertEqual([track["kind"] for track in events[0]["tracks"]], ["video", "audio"])
                self.assertEqual(state.peak, 2)
                for track in events[0]["tracks"]:
                    self.assertGreater((workdir / track["file"]).stat().st_size, 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
