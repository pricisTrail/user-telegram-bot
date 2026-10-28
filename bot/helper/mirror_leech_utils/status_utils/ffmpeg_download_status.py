from ...ext_utils.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)


class FFmpegDownloadStatus:
    def __init__(self, listener, downloader, gid):
        self.listener = listener
        self._downloader = downloader
        self._gid = gid
        self.tool = "ffmpeg"

    def gid(self):
        return self._gid

    def name(self):
        return self.listener.name

    def size(self):
        estimate = self._downloader.runner.estimated_size
        return f"~{get_readable_file_size(estimate)}" if estimate else "Unknown"

    def processed_bytes(self):
        return get_readable_file_size(self._downloader.runner.processed_bytes)

    def progress(self):
        return f"{round(self._downloader.runner.progress_raw, 2)}%"

    def speed(self):
        return f"{get_readable_file_size(self._downloader.runner.speed_raw)}/s"

    def eta(self):
        value = self._downloader.runner.eta_raw
        return get_readable_time(value) if value else "-"

    def status(self):
        return MirrorStatus.STATUS_DOWNLOAD

    def task(self):
        return self._downloader
