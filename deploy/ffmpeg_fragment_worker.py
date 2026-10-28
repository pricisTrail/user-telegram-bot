"""Download static DASH tracks with two native yt-dlp fragment workers.

The parent owns the deadline, disk budget, cleanup and local FFmpeg merge.
Only numeric media metadata and fixed local basenames cross stdout. Requests
arrive on stdin so signed media URLs never appear in the process arguments.
"""

from contextlib import redirect_stderr, redirect_stdout
import json
import math
from pathlib import Path
import sys
from threading import Lock
from time import monotonic
from urllib.parse import urljoin, urlsplit


MAX_REQUEST = 16 * 1024
MAX_FORMATS = 128
MAX_FRAGMENTS = 20_000
MAX_FRAGMENT_BYTES = 16 * 1024 * 1024
WORKERS = 2


class UnsupportedMedia(Exception):
    pass


class QuietLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


class NullWriter:
    def write(self, value):
        return len(value)

    def flush(self):
        pass


def read_fragment_bounded(downloader, context):
    """Native yt-dlp otherwise reads a complete fragment into memory at once."""
    filename = context.get("fragment_filename_sanitized")
    if not filename:
        return None
    stream, sanitized = downloader.sanitize_open(filename, "rb")
    context["fragment_filename_sanitized"] = sanitized
    with stream:
        content = stream.read(MAX_FRAGMENT_BYTES + 1)
    if len(content) > MAX_FRAGMENT_BYTES:
        raise RuntimeError()
    return content


def positive_number(value):
    if isinstance(value, bool):
        return 0
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def validate_http_url(value):
    if not isinstance(value, str) or not value:
        raise UnsupportedMedia()
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username and not parsed.password
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )
        parsed.port
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise UnsupportedMedia()
    return value


def _is_video(fmt):
    return fmt.get("vcodec") not in {None, "none"} or positive_number(fmt.get("height")) > 0


def _is_audio(fmt):
    return fmt.get("acodec") not in {None, "none"} and not _is_video(fmt)


def _validate_track(fmt):
    if (
        fmt.get("protocol") != "http_dash_segments"
        or fmt.get("is_live")
        or fmt.get("has_drm")
        or fmt.get("requested_formats")
        or fmt.get("hls_aes")
        or fmt.get("decryption_key")
    ):
        raise UnsupportedMedia()
    validate_http_url(fmt.get("url"))
    base = fmt.get("fragment_base_url") or fmt["url"]
    validate_http_url(base)
    fragments = fmt.get("fragments")
    if not isinstance(fragments, list) or not 0 < len(fragments) <= MAX_FRAGMENTS:
        raise UnsupportedMedia()
    for fragment in fragments:
        if not isinstance(fragment, dict):
            raise UnsupportedMedia()
        fragment_url = fragment.get("url") or fragment.get("path")
        if not isinstance(fragment_url, str) or not fragment_url:
            raise UnsupportedMedia()
        validate_http_url(urljoin(base, fragment_url))


def build_plan(raw, max_height, expected_duration):
    """Validate every selected fragment before any media bytes are fetched."""
    if (
        not isinstance(raw, dict)
        or raw.get("_type", "video") != "video"
        or "entries" in raw
        or raw.get("is_live")
        or raw.get("live_status") not in {None, "not_live", "was_live"}
        or raw.get("has_drm")
    ):
        raise UnsupportedMedia()
    formats = raw.get("formats")
    if not isinstance(formats, list) or not 0 < len(formats) <= MAX_FORMATS:
        raise UnsupportedMedia()
    formats = [fmt for fmt in formats if isinstance(fmt, dict)]
    videos = [fmt for fmt in formats if _is_video(fmt)]
    chosen = []
    if videos:
        candidates = [fmt for fmt in videos if (
            not max_height or 0 < positive_number(fmt.get("height")) <= max_height
        )]
        if not candidates:
            raise UnsupportedMedia()
        chosen.append(max(candidates, key=lambda fmt: (
            positive_number(fmt.get("height")),
            positive_number(fmt.get("vbr")) or positive_number(fmt.get("tbr")),
            positive_number(fmt.get("width")),
        )))
    audio = [fmt for fmt in formats if _is_audio(fmt)]
    if audio and (not chosen or chosen[0].get("acodec") in {None, "none"}):
        chosen.append(max(audio, key=lambda fmt: (
            positive_number(fmt.get("abr")) or positive_number(fmt.get("tbr")),
            positive_number(fmt.get("asr")),
        )))
    if not chosen:
        raise UnsupportedMedia()
    duration = positive_number(raw.get("duration")) or expected_duration
    if abs(duration - expected_duration) > max(2.0, expected_duration * 0.001):
        raise UnsupportedMedia()
    tracks = []
    safe_tracks = []
    for index, fmt in enumerate(chosen):
        _validate_track(fmt)
        kind = "video" if _is_video(fmt) else "audio"
        rate = positive_number(fmt.get("tbr"))
        size = int(positive_number(fmt.get("filesize")) or positive_number(fmt.get("filesize_approx")))
        if not size and rate:
            size = int(rate * 1000 * duration / 8 * 1.03)
        safe_tracks.append({
            "file": f"track{index}.bin", "kind": kind,
            "width": int(positive_number(fmt.get("width"))) if kind == "video" else 0,
            "height": int(positive_number(fmt.get("height"))) if kind == "video" else 0,
            "duration": duration, "estimated_size": size,
        })
        track = dict(fmt)
        for key in ("requested_formats", "requested_downloads", "entries", "formats", "filepath"):
            track.pop(key, None)
        track["is_live"] = False
        headers = {**(raw.get("http_headers") or {}), **(fmt.get("http_headers") or {})}
        if headers:
            track["http_headers"] = headers
        tracks.append(track)
    video = next((track for track in safe_tracks if track["kind"] == "video"), {})
    known_sizes = [track["estimated_size"] for track in safe_tracks]
    plan = {
        "event": "plan", "tracks": safe_tracks, "duration": duration,
        "estimated_size": sum(known_sizes) if all(known_sizes) else 0,
        "width": video.get("width", 0), "height": video.get("height", 0),
    }
    return plan, tracks


class Emitter:
    def __init__(self, stream):
        self.stream = stream
        self.lock = Lock()
        self.last_progress = {}

    def _write(self, event):
        self.stream.write(json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n")
        self.stream.flush()

    def emit(self, event):
        with self.lock:
            self._write(event)

    def hook(self, index):
        def progress(data):
            status = data.get("status")
            if status not in {"downloading", "finished"}:
                return
            now = monotonic()
            with self.lock:
                if status != "finished" and now - self.last_progress.get(index, -1) < 0.5:
                    return
                self.last_progress[index] = now
                self._write({
                    "event": "progress", "track": index, "status": status,
                    "downloaded_bytes": int(positive_number(data.get("downloaded_bytes"))),
                    "total_bytes": int(positive_number(data.get("total_bytes")) or positive_number(data.get("total_bytes_estimate"))),
                    "fragment_index": int(positive_number(data.get("fragment_index"))),
                    "fragment_count": int(positive_number(data.get("fragment_count"))),
                })
        return progress


def download(request, input_stream, emitter, sdk=None):
    source = validate_http_url(request.get("url"))
    if not urlsplit(source).path.lower().endswith(".mpd"):
        raise UnsupportedMedia()
    max_height = request.get("max_height", 720)
    if isinstance(max_height, bool) or not isinstance(max_height, int) or not 0 <= max_height <= 4320:
        raise ValueError()
    expected_duration = positive_number(request.get("duration"))
    if not expected_duration or request.get("workers", WORKERS) != WORKERS:
        raise ValueError()
    workdir = Path(request["workdir"]).resolve(strict=True)
    if not workdir.is_dir():
        raise ValueError()
    if sdk is None:
        from yt_dlp import YoutubeDL
        from yt_dlp.downloader.dash import DashSegmentsFD
    else:
        YoutubeDL, DashSegmentsFD = sdk

    class BoundedDashSegmentsFD(DashSegmentsFD):
        def _read_fragment(self, context):
            return read_fragment_bounded(self, context)

    options = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "logger": QuietLogger(), "cachedir": False, "usenetrc": False,
        "cookiefile": None, "noplaylist": True, "external_downloader": "native",
        "allowed_extractors": ["generic"], "enable_file_urls": False,
        "allow_unplayable_formats": False,
        "fixup": "never", "postprocessors": [], "concurrent_fragment_downloads": WORKERS,
        "skip_unavailable_fragments": False, "keep_fragments": False,
        "continuedl": False, "overwrites": False, "_no_ytdl_file": True,
        "retries": 2, "fragment_retries": 2, "socket_timeout": 20,
    }
    # Also discard unexpected SDK prints: diagnostic text can contain URLs.
    sink = NullWriter()
    with redirect_stdout(sink), redirect_stderr(sink), YoutubeDL(options) as ydl:
        raw = ydl.extract_info(source, download=False, process=False)
        plan, tracks = build_plan(raw, max_height, expected_duration)
        paths = [workdir / track["file"] for track in plan["tracks"]]
        if any(path.exists() for path in paths):
            raise ValueError()
        emitter.emit(plan)
        if input_stream.readline(16).strip() != "go":
            raise ValueError()
        for index, (track, path) in enumerate(zip(tracks, paths)):
            downloader = BoundedDashSegmentsFD(ydl, options)
            downloader.add_progress_hook(emitter.hook(index))
            result = downloader.download(str(path), track)
            success = result[0] if isinstance(result, tuple) else result
            if not success or not path.is_file() or path.stat().st_size <= 0:
                raise RuntimeError()
        emitter.emit({"event": "complete"})


def main(input_stream=None, output_stream=None, sdk=None):
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    emitter = Emitter(output_stream)
    try:
        line = input_stream.readline(MAX_REQUEST + 1)
        if len(line) > MAX_REQUEST:
            raise ValueError()
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError()
        download(request, input_stream, emitter, sdk)
        return 0
    except UnsupportedMedia:
        emitter.emit({"event": "unsupported"})
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        try:
            emitter.emit({"event": "error"})
        except (BrokenPipeError, OSError):
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
