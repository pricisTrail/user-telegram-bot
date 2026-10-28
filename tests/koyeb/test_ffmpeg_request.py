import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[2] / "bot/helper/ext_utils/ffmpeg_request.py"
spec = importlib.util.spec_from_file_location("ffmpeg_request_under_test", MODULE_PATH)
request = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = request
spec.loader.exec_module(request)


class FFmpegRequestTests(unittest.TestCase):
    def test_dash_url_defaults_to_720_and_keeps_signed_query(self):
        url = "https://media.example.org/manifest.mpd?token=private-example&x=1"
        result = request.parse_ffmpeg_request("/ffl " + url)
        self.assertEqual(result.url, url)
        self.assertEqual(result.max_height, 720)
        self.assertNotIn("private-example", repr(result))

    def test_reply_quality_and_quoted_name(self):
        result = request.parse_ffmpeg_request(
            '/ffl@MyBot -q best -n "A video.mkv"', "https://media.example.org/watch.m3u8"
        )
        self.assertEqual((result.max_height, result.name), (0, "A video.mkv"))

    def test_plain_name_gets_mp4_extension(self):
        self.assertEqual(
            request.parse_ffmpeg_request('/ffl https://example.org/video -n "My video"').name,
            "My video.mp4",
        )

    def test_local_protocols_ips_and_embedded_credentials_are_rejected(self):
        for url in (
            "file:///etc/passwd", "concat:https://example.org/a", "http://127.0.0.1/a",
            "http://[::1]/a", "http://10.0.0.1/a", "http://localhost/a",
            "https://user:private-example@example.org/a", "https://example.org:99999/a",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                request.parse_ffmpeg_request("/ffl " + url)
            self.assertNotIn("private-example", str(caught.exception))

    def test_filename_cannot_escape_task_directory(self):
        for name in ("../video.mp4", "a/b.mp4", "a\\b.mp4", ".hidden.mp4", "video.exe"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                request.safe_output_name(name)

    def test_extra_ffmpeg_flags_and_extra_urls_are_rejected(self):
        for extra in ("-i private-example", "-q 720 -q best", "https://example.net/other"):
            with self.subTest(extra=extra), self.assertRaises(ValueError) as caught:
                request.parse_ffmpeg_request("/ffl https://example.org/media " + extra)
            self.assertNotIn("private-example", str(caught.exception))

    def test_invalid_quality_and_missing_values(self):
        for command in ("/ffl", "/ffl -q", "/ffl https://example.org/a -q 0", "/ffl https://example.org/a -q giant"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                request.parse_ffmpeg_request(command)


if __name__ == "__main__":
    unittest.main()
