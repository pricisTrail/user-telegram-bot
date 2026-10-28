"""Bounded native DASH fragments followed by a local FFmpeg stream copy."""

import asyncio
from asyncio.subprocess import PIPE
import json
import math
from pathlib import Path
from shutil import disk_usage, rmtree
import sys
from tempfile import mkdtemp
from time import monotonic
from urllib.parse import urlsplit

from .ffmpeg_download import (
    FFmpegDownloadRunner, FFmpegDownloadError, FFmpegDownloadCancelled,
    select_streams,
)


class ParallelFFmpegDownloadRunner(FFmpegDownloadRunner):
    def __init__(self, *, parallel_fragments=2, **kwargs):
        super().__init__(**kwargs)
        # A fixed small pool protects the free instance from unbounded workers.
        self.parallel_fragments = 2 if int(parallel_fragments) > 1 else 1
        self._fragment_plan = None
        self._fragment_complete = False
        self._fragment_unsupported = False
        self._fragment_bytes = {}
        self._fragment_fractions = {}

    def _check_parallel_disk(self, output, workspace):
        used = output.stat().st_size if output.is_file() else 0
        for item in workspace.rglob("*"):
            try:
                if item.is_file():
                    used += item.stat().st_size
            except FileNotFoundError:
                # Native workers remove each temporary fragment after appending.
                continue
        if used > self._output_budget or disk_usage(output.parent).free < self.disk_reserve:
            raise FFmpegDownloadError(
                "Download stopped to preserve 256 MiB of free disk space. Use a smaller source or quality."
            )

    async def _monitor_parallel_disk(self, process, output, workspace):
        while process.returncode is None:
            self._check_parallel_disk(output, workspace)
            await asyncio.sleep(self.sample_seconds)

    @staticmethod
    def _positive(value):
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0
        )

    def _validate_plan(self, event, max_height, selection):
        tracks = event.get("tracks")
        if (
            not isinstance(tracks, list) or not 1 <= len(tracks) <= 2
            or not self._positive(event.get("duration"))
        ):
            raise FFmpegDownloadError("The parallel downloader returned invalid media information.")
        kinds = []
        for index, track in enumerate(tracks):
            if not isinstance(track, dict) or track.get("file") != f"track{index}.bin":
                raise FFmpegDownloadError("The parallel downloader returned invalid media information.")
            kind = track.get("kind")
            if kind not in {"video", "audio"} or kind in kinds:
                raise FFmpegDownloadError("The parallel downloader returned invalid media information.")
            kinds.append(kind)
            if kind == "video" and (
                not self._positive(track.get("height"))
                or not self._positive(track.get("width"))
                or (max_height and track["height"] > max_height)
            ):
                raise FFmpegDownloadError("The parallel downloader selected an unexpected video quality.")
        if "video" not in kinds or (len(selection.stream_indices) == 2 and "audio" not in kinds):
            raise FFmpegDownloadError("The parallel downloader could not select the required video and audio.")
        estimate = event.get("estimated_size", 0)
        if not self._positive(estimate):
            estimate = selection.estimated_size
        # Track files and the merged output coexist until validation succeeds.
        if estimate and estimate * 2 > self._output_budget:
            raise FFmpegDownloadError(
                "Insufficient disk space for the tracks and merged file while retaining the 256 MiB reserve. "
                "Choose a smaller source or quality."
            )
        return tracks

    async def _fragment_events(self, reader, process, max_height, selection):
        while line := await reader.readline():
            if self.cancelled:
                raise FFmpegDownloadCancelled("Download cancelled by user.")
            if len(line) > 8192:
                raise FFmpegDownloadError("The parallel downloader returned invalid progress data.")
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                raise FFmpegDownloadError("The parallel downloader returned invalid progress data.") from None
            if not isinstance(event, dict):
                raise FFmpegDownloadError("The parallel downloader returned invalid progress data.")
            kind = event.get("event")
            if kind == "unsupported" and self._fragment_plan is None:
                self._fragment_unsupported = True
            elif kind == "plan" and self._fragment_plan is None and not self._fragment_unsupported:
                self._validate_plan(event, max_height, selection)
                self._fragment_plan = event
                self.estimated_size = (
                    int(event["estimated_size"])
                    if self._positive(event.get("estimated_size")) else selection.estimated_size
                )
                process.stdin.write(b"go\n")
                await process.stdin.drain()
            elif kind == "progress" and self._fragment_plan is not None:
                index = event.get("track")
                tracks = self._fragment_plan["tracks"]
                if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(tracks):
                    raise FFmpegDownloadError("The parallel downloader returned invalid progress data.")
                downloaded = event.get("downloaded_bytes", 0)
                if not isinstance(downloaded, (int, float)) or not math.isfinite(downloaded) or downloaded < 0:
                    raise FFmpegDownloadError("The parallel downloader returned invalid progress data.")
                self._fragment_bytes[index] = max(self._fragment_bytes.get(index, 0), int(downloaded))
                self.processed_bytes = max(self.processed_bytes, sum(self._fragment_bytes.values()))
                total = event.get("total_bytes", 0)
                fragment, count = event.get("fragment_index", 0), event.get("fragment_count", 0)
                fraction = 0
                if event.get("status") == "finished":
                    fraction = 1
                elif self._positive(total):
                    fraction = min(1, downloaded / total)
                elif self._positive(count) and self._positive(fragment):
                    fraction = min(1, fragment / count)
                self._fragment_fractions[index] = max(self._fragment_fractions.get(index, 0), fraction)
                weights = [track.get("estimated_size", 0) for track in tracks]
                if not all(self._positive(weight) for weight in weights):
                    weights = [1] * len(tracks)
                ratio = sum(weights[i] * self._fragment_fractions.get(i, 0) for i in range(len(tracks))) / sum(weights)
                self.processed_time = max(self.processed_time, self.duration * ratio * 0.95)
            elif kind == "complete" and self._fragment_plan is not None:
                self._fragment_complete = True
            else:
                raise FFmpegDownloadError("The parallel media download failed. Check the source and retry.")

    async def _watched_process(self, process, handler, output, workspace):
        io_task = asyncio.create_task(self._run_io(process, handler))
        monitor = asyncio.create_task(self._monitor_parallel_disk(process, output, workspace))
        try:
            done, _ = await asyncio.wait({io_task, monitor}, return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor
            await io_task
            if self.cancelled:
                raise FFmpegDownloadCancelled("Download cancelled by user.")
            if process.returncode != 0:
                raise FFmpegDownloadError("The parallel media download or local merge failed. Check the source and retry.")
            self._check_parallel_disk(output, workspace)
        finally:
            await self._kill_and_wait(process)
            for task in (io_task, monitor):
                if not task.done():
                    task.cancel()
            await asyncio.gather(io_task, monitor, return_exceptions=True)

    async def _inspect_output(self, output, max_height, expected_audio):
        process = await self._spawn(
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,crypto,data",
            "-show_entries", "stream=index,codec_type,width,height,duration:stream_tags=rotate:stream_side_data=rotation:format=duration",
            "-of", "json", "-i", str(output),
        )
        try:
            raw = await self._run_io(process, self._capture_probe, self.probe_timeout)
        except asyncio.TimeoutError:
            raise FFmpegDownloadError("Inspection of the merged media file timed out.") from None
        if process.returncode != 0:
            raise FFmpegDownloadError("The merged media file could not be verified.")
        try:
            metadata = json.loads(raw)
            actual = select_streams(metadata, max_height)
        except (ValueError, UnicodeDecodeError):
            raise FFmpegDownloadError("The merged media file could not be verified.") from None
        if expected_audio and not any(
            stream.get("codec_type") == "audio" for stream in metadata.get("streams", []) if isinstance(stream, dict)
        ):
            raise FFmpegDownloadError("The merged media file is missing its expected audio track.")
        if not any(
            stream.get("codec_type") == "video" for stream in metadata.get("streams", []) if isinstance(stream, dict)
        ):
            raise FFmpegDownloadError("The merged media file is missing its expected video track.")
        tolerance = max(2.0, self.duration * 0.001)
        if actual.duration < self.duration - tolerance:
            raise FFmpegDownloadError("The media transfer ended before its expected duration. The incomplete file was removed.")
        for stream in metadata.get("streams", []):
            if not isinstance(stream, dict) or stream.get("codec_type") not in {"video", "audio"}:
                continue
            try:
                track_duration = float(stream.get("duration", 0))
            except (TypeError, ValueError, OverflowError):
                track_duration = 0
            if self._positive(track_duration) and track_duration < self.duration - tolerance:
                raise FFmpegDownloadError("A media track ended before its expected duration. The incomplete file was removed.")
        return actual

    async def _download_parallel(self, url, output, max_height, selection, workspace):
        self._started = monotonic()
        process = await self._spawn(sys.executable, "-m", "deploy.ffmpeg_fragment_worker", stdin=PIPE)
        request = {
            "url": url, "workdir": str(workspace), "max_height": max_height,
            "duration": selection.duration, "workers": self.parallel_fragments,
        }
        watched = False
        try:
            process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
            await process.stdin.drain()
            watched = True
            await self._watched_process(
                process, lambda reader: self._fragment_events(reader, process, max_height, selection), output, workspace
            )
        except (BrokenPipeError, ConnectionResetError):
            if self.cancelled:
                raise FFmpegDownloadCancelled("Download cancelled by user.") from None
            raise FFmpegDownloadError("The parallel media download stopped unexpectedly.") from None
        finally:
            if not watched:
                # No I/O task owns these pipes if the initial handshake fails.
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await asyncio.gather(
                    self._discard(process.stdout), self._discard(process.stderr), process.wait()
                )
            process.stdin.close()
        if self._fragment_unsupported and self._fragment_plan is None:
            return None
        if not self._fragment_complete or self._fragment_plan is None:
            raise FFmpegDownloadError("The parallel media download ended before completion.")
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin", "-n"]
        tracks = self._fragment_plan["tracks"]
        for track in tracks:
            source = workspace / track["file"]
            if not source.is_file() or source.stat().st_size == 0:
                raise FFmpegDownloadError("The parallel downloader did not create every media track.")
            args.extend(["-protocol_whitelist", "file,crypto,data", "-threads", "1", "-i", str(source)])
        for index, track in enumerate(tracks):
            args.extend(["-map", f"{index}:{'v' if track['kind'] == 'video' else 'a'}:0"])
        args.extend(["-c", "copy", "-threads", "1", "-progress", "pipe:1", "-nostats", str(output)])
        process = await self._spawn(*args)
        await self._watched_process(process, self._read_progress, output, workspace)
        if not output.is_file() or output.stat().st_size == 0:
            raise FFmpegDownloadError("FFmpeg finished without creating a nonempty media file.")
        actual = await self._inspect_output(output, max_height, any(track["kind"] == "audio" for track in tracks))
        self.processed_time = self.duration
        self.processed_bytes = output.stat().st_size
        return actual

    async def _download(self, url, output, max_height):
        if self.parallel_fragments <= 1 or not urlsplit(url).path.lower().endswith(".mpd"):
            return await super()._download(url, output, max_height)
        selection = await self.probe(url, max_height)
        if not selection.width or not selection.height:
            return await super()._download(url, output, max_height)
        self.duration = selection.duration
        self.estimated_size = selection.estimated_size
        self.selected_height = selection.height
        self._output_budget = disk_usage(output.parent).free - self.disk_reserve
        if self._output_budget <= 0:
            raise FFmpegDownloadError("Insufficient disk space while retaining the 256 MiB reserve.")
        workspace = Path(mkdtemp(prefix=".ffl-fragments-", dir=output.parent))
        try:
            result = await self._download_parallel(url, output, max_height, selection, workspace)
        finally:
            # Only the freshly created child directory belongs to this runner.
            if workspace.resolve().parent != output.parent.resolve() or not workspace.name.startswith(".ffl-fragments-"):
                raise FFmpegDownloadError("Unable to safely clean temporary media tracks.")
            try:
                rmtree(workspace)
            except FileNotFoundError:
                # Task cancellation can already have removed the task directory.
                pass
        if result is not None:
            return result
        return await super()._download(url, output, max_height)
