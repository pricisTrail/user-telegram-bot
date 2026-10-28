"""Bounded, stream-copy HTTP media downloads; this module has no bot imports."""

import asyncio
from asyncio.subprocess import DEVNULL, PIPE
from dataclasses import dataclass
import json
import math
from pathlib import Path
from shutil import disk_usage
from time import monotonic
from urllib.parse import urlsplit


HTTP_PROTOCOLS = "http,https,tcp,tls"
PROBE_STDOUT_LIMIT = 2 * 1024 * 1024
STDERR_LIMIT = 64 * 1024
DISK_RESERVE = 256 * 1024 * 1024


class FFmpegDownloadError(Exception):
    """A deliberately safe message that never contains remote diagnostics."""


class FFmpegDownloadCancelled(FFmpegDownloadError):
    pass


@dataclass(frozen=True)
class MediaSelection:
    stream_indices: tuple
    duration: float
    estimated_size: int = 0
    height: int = 0
    width: int = 0


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def _display_dimensions(stream):
    """Use display orientation without guessing at an unknown transform."""
    width = int(_number(stream.get("width")))
    height = int(_number(stream.get("height")))
    rotations = []
    side_data = stream.get("side_data_list", [])
    if not isinstance(side_data, list):
        return 0, height
    for entry in side_data:
        if isinstance(entry, dict) and "rotation" in entry:
            rotations.append(entry["rotation"])
    tags = stream.get("tags", {})
    if isinstance(tags, dict) and "rotate" in tags:
        rotations.append(tags["rotate"])
    orientations = set()
    for value in rotations:
        try:
            angle = float(value)
            if isinstance(value, bool) or not math.isfinite(angle) or angle % 90:
                return 0, height
            orientations.add(int(angle // 90) % 2)
        except (ValueError, TypeError, OverflowError):
            return 0, height
    if len(orientations) > 1:
        return 0, height
    if width and height and orientations == {1}:
        return height, width
    return width, height


def select_streams(metadata, max_height=720):
    """Choose an existing video representation and audio, without encoding."""
    if not isinstance(max_height, int) or max_height < 0:
        raise FFmpegDownloadError("The video height must be a nonnegative integer.")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("streams"), list):
        raise FFmpegDownloadError("FFprobe returned invalid media information.")
    streams = [
        stream for stream in metadata["streams"]
        if isinstance(stream, dict)
        and isinstance(stream.get("index"), int)
        and stream["index"] >= 0
    ]
    videos = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
        and not (
            isinstance(stream.get("disposition"), dict)
            and stream["disposition"].get("attached_pic") in (1, "1")
        )
    ]
    candidates = [
        stream for stream in videos
        if not max_height or 0 < _number(stream.get("height")) <= max_height
    ]
    selected = []
    height = 0
    width = 0
    if videos:
        if not candidates:
            raise FFmpegDownloadError(
                f"No video stream at or below {max_height}p is available. "
                "Choose a higher limit or best quality; FFmpeg will not transcode."
            )
        video = max(candidates, key=lambda stream: (
            _number(stream.get("height")), _number(stream.get("bit_rate")),
            -stream["index"],
        ))
        selected.append(video)
        width, height = _display_dimensions(video)
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if audio:
        selected.append(max(audio, key=lambda stream: (
            _number(stream.get("bit_rate")), -stream["index"],
        )))
    if not selected:
        raise FFmpegDownloadError("No downloadable video or audio stream was found.")
    media_format = metadata.get("format")
    duration = _number(media_format.get("duration")) if isinstance(media_format, dict) else 0
    if not duration:
        duration = max((_number(stream.get("duration")) for stream in selected), default=0)
    if not duration:
        raise FFmpegDownloadError(
            "This source has no finite duration. Live streams are not supported."
        )
    rates = [_number(stream.get("bit_rate")) for stream in selected]
    # Unknown rates remain unknown: do not present an incomplete estimate as a total.
    estimated_size = int(sum(rates) * duration / 8 * 1.03) if all(rates) else 0
    return MediaSelection(
        tuple(stream["index"] for stream in selected), duration, estimated_size, height, width
    )


def validate_http_url(url):
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and not parsed.username and not parsed.password
            and not any(ord(char) < 32 or ord(char) == 127 for char in url)
        )
        parsed.port  # Validate malformed ports without echoing the URL.
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise FFmpegDownloadError("Provide a valid HTTP or HTTPS media URL.")


class FFmpegDownloadRunner:
    def __init__(self, *, process_callback=None, probe_timeout=45, max_seconds=3600,
                 disk_reserve=DISK_RESERVE, sample_seconds=0.5):
        self.process_callback = process_callback
        self.probe_timeout = probe_timeout
        self.max_seconds = max_seconds
        self.disk_reserve = disk_reserve
        self.sample_seconds = sample_seconds
        self.process = None
        self.cancelled = False
        self.processed_bytes = 0
        self.processed_time = 0
        self.duration = 0
        self.estimated_size = 0
        self.selected_height = 0
        self._started = 0
        self._output_budget = 0
        self._stderr_tail = b""

    @property
    def progress_raw(self):
        return min(100.0, max(0.0, self.processed_time * 100 / self.duration)) if self.duration else 0.0

    @property
    def speed_raw(self):
        elapsed = monotonic() - self._started if self._started else 0
        return self.processed_bytes / elapsed if elapsed > 0 else 0

    @property
    def eta_raw(self):
        if self.processed_time > 0 and self._started:
            rate = self.processed_time / max(0.001, monotonic() - self._started)
            return max(0.0, self.duration - self.processed_time) / rate
        return 0

    async def _spawn(self, *args):
        if self.cancelled:
            raise FFmpegDownloadCancelled("Download cancelled by user.")
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdin=DEVNULL, stdout=PIPE, stderr=PIPE
            )
        except FileNotFoundError:
            raise FFmpegDownloadError(f"{args[0]} is not installed in this deployment.") from None
        except OSError:
            raise FFmpegDownloadError(f"Unable to start {args[0]} in this deployment.") from None
        self.process = process
        if self.process_callback:
            self.process_callback(process)
        # Cancellation can arrive while the operating system starts the child.
        if self.cancelled:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # No I/O worker owns these streams yet. Reading after kill is
            # necessary even here: Process.wait can wait for pipe transports.
            await asyncio.gather(
                self._discard(process.stdout), self._discard(process.stderr), process.wait()
            )
            raise FFmpegDownloadCancelled("Download cancelled by user.")
        return process

    async def _kill_and_wait(self, process=None):
        process = process or self.process
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()

    async def cancel(self):
        self.cancelled = True
        await self._kill_and_wait()

    async def _drain_stderr(self, reader):
        while chunk := await reader.read(8192):
            self._stderr_tail = (self._stderr_tail + chunk)[-STDERR_LIMIT:]

    async def _capture_probe(self, reader):
        output = bytearray()
        while chunk := await reader.read(8192):
            if len(output) + len(chunk) > PROBE_STDOUT_LIMIT:
                raise FFmpegDownloadError("Media information exceeds the supported size limit.")
            output.extend(chunk)
        return bytes(output)

    async def _discard(self, reader):
        while await reader.read(8192):
            pass

    async def _run_io(self, process, stdout_handler, timeout=None):
        tasks = [
            asyncio.create_task(stdout_handler(process.stdout)),
            asyncio.create_task(self._drain_stderr(process.stderr)),
            asyncio.create_task(process.wait()),
        ]
        try:
            result = await asyncio.wait_for(asyncio.gather(*tasks), timeout)
            return result[0]
        except BaseException:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # A size-limit failure stops the capturing reader. Drain both pipes
            # after cancelling their old readers so process.wait cannot deadlock
            # on a full subprocess pipe during cleanup.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(
                self._discard(process.stdout), self._discard(process.stderr), process.wait()
            )
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def probe(self, url, max_height=720):
        validate_http_url(url)
        process = await self._spawn(
            "ffprobe", "-v", "error", "-protocol_whitelist", HTTP_PROTOCOLS,
            "-show_entries", "stream=index,codec_type,width,height,bit_rate,duration:stream_disposition=attached_pic:stream_tags=rotate:stream_side_data=rotation:format=duration",
            "-of", "json", "-i", url,
        )
        try:
            raw = await self._run_io(process, self._capture_probe, self.probe_timeout)
        except asyncio.TimeoutError:
            raise FFmpegDownloadError("Media inspection timed out after 45 seconds.") from None
        if self.cancelled:
            raise FFmpegDownloadCancelled("Download cancelled by user.")
        if process.returncode != 0:
            raise FFmpegDownloadError(
                "FFprobe could not inspect this source. Check that the URL is a direct, accessible media link."
            )
        try:
            metadata = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise FFmpegDownloadError("FFprobe returned invalid media information.") from None
        return select_streams(metadata, max_height)

    def _progress_line(self, line):
        key, separator, value = line.partition("=")
        if not separator:
            return
        if key == "total_size":
            self.processed_bytes = max(self.processed_bytes, int(_number(value)))
        elif key in {"out_time_us", "out_time_ms"}:
            # FFmpeg's historical out_time_ms field is also measured in microseconds.
            self.processed_time = max(self.processed_time, _number(value) / 1_000_000)

    async def _read_progress(self, reader):
        pending = b""
        while chunk := await reader.read(8192):
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                self._progress_line(line.decode("utf-8", errors="ignore").strip())
            if len(pending) > 8192:
                pending = b""
        if pending:
            self._progress_line(pending.decode("utf-8", errors="ignore").strip())

    def _check_disk(self, output):
        actual = output.stat().st_size if output.exists() else 0
        self.processed_bytes = max(self.processed_bytes, actual)
        if actual > self._output_budget or disk_usage(output.parent).free < self.disk_reserve:
            raise FFmpegDownloadError(
                "Download stopped to preserve 256 MiB of free disk space. Use a smaller source or quality."
            )

    async def _monitor_disk(self, process, output):
        while process.returncode is None:
            self._check_disk(output)
            await asyncio.sleep(self.sample_seconds)

    async def _download(self, url, output, max_height):
        selection = await self.probe(url, max_height)
        self.duration = selection.duration
        self.estimated_size = selection.estimated_size
        self.selected_height = selection.height
        self._output_budget = disk_usage(output.parent).free - self.disk_reserve
        if self._output_budget <= 0 or self.estimated_size > self._output_budget:
            raise FFmpegDownloadError(
                "Insufficient disk space for this download while retaining the 256 MiB reserve. Choose a smaller source or quality."
            )
        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin", "-n",
            "-protocol_whitelist", HTTP_PROTOCOLS, "-threads", "1", "-i", url,
        ]
        for index in selection.stream_indices:
            args.extend(["-map", f"0:{index}"])
        args.extend(["-c", "copy", "-threads", "1", "-progress", "pipe:1", "-nostats", str(output)])
        self._started = monotonic()
        process = await self._spawn(*args)
        io_task = asyncio.create_task(self._run_io(process, self._read_progress))
        monitor = asyncio.create_task(self._monitor_disk(process, output))
        try:
            done, _ = await asyncio.wait({io_task, monitor}, return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor  # Propagate a disk limit before treating exit code zero as success.
            await io_task
            if self.cancelled:
                raise FFmpegDownloadCancelled("Download cancelled by user.")
            if process.returncode != 0:
                raise FFmpegDownloadError(
                    "FFmpeg could not copy this media into the selected container. "
                    "Check the URL or try an output filename ending in .mkv; no transcoding was attempted."
                )
            self._check_disk(output)
            if not output.is_file() or output.stat().st_size == 0:
                raise FFmpegDownloadError("FFmpeg finished without creating a nonempty media file.")
            tolerance = max(2.0, self.duration * 0.001)
            if self.processed_time <= 0 or self.processed_time < self.duration - tolerance:
                raise FFmpegDownloadError(
                    "The media transfer ended before its expected duration. "
                    "The incomplete file was removed; retry with a working source."
                )
            self.processed_time = self.duration
            self.processed_bytes = output.stat().st_size
            return selection
        finally:
            await self._kill_and_wait(process)
            for task in (io_task, monitor):
                if not task.done():
                    task.cancel()
            await asyncio.gather(io_task, monitor, return_exceptions=True)

    async def run(self, url, output_path, max_height=720):
        output = Path(output_path)
        if output.suffix.lower() not in {".mp4", ".mkv"}:
            raise FFmpegDownloadError("The output filename must end in .mp4 or .mkv.")
        if output.exists():
            raise FFmpegDownloadError("The output file already exists; choose another filename.")
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            return await asyncio.wait_for(self._download(url, output, max_height), self.max_seconds)
        except BaseException as error:
            await self._kill_and_wait()
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(error, asyncio.TimeoutError):
                raise FFmpegDownloadError("Download exceeded the one-hour time limit and was stopped.") from None
            if isinstance(error, (FFmpegDownloadError, asyncio.CancelledError)):
                raise
            raise FFmpegDownloadError("FFmpeg download failed. Check the media URL and available disk space.") from None
