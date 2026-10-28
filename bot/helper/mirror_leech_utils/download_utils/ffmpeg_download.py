"""Connect the bounded FFmpeg runner to the bot's normal task lifecycle."""

from asyncio import Lock
from pathlib import Path
from secrets import token_urlsafe

from .... import task_dict, task_dict_lock
from ...ext_utils.ffmpeg_download import (
    FFmpegDownloadRunner,
    FFmpegDownloadError,
    FFmpegDownloadCancelled,
)
from ...ext_utils.task_manager import check_running_tasks
from ..status_utils.ffmpeg_download_status import FFmpegDownloadStatus
from ..status_utils.queue_status import QueueStatus
from ...telegram_helper.message_utils import send_status_message


class FFmpegDownloader:
    def __init__(self, listener, path, max_height=720):
        self.listener = listener
        self.path = Path(path) / listener.name
        self.max_height = max_height
        self.runner = FFmpegDownloadRunner(process_callback=self._set_process)
        self._error_reported = False
        self._error_lock = Lock()

    def _set_process(self, process):
        self.listener.subproc = process

    async def _fail(self, message):
        async with self._error_lock:
            if self._error_reported:
                return
            self._error_reported = True
        await self.listener.on_download_error(message)

    async def download(self):
        try:
            await self.runner.run(self.listener.link, self.path, self.max_height)
        except FFmpegDownloadCancelled:
            await self._fail("FFmpeg download cancelled by user.")
            return
        except FFmpegDownloadError as error:
            await self._fail(str(error))
            return
        if self.listener.is_cancelled:
            await self._fail("FFmpeg download cancelled by user.")
            return
        self.listener.subproc = None
        self.listener.size = self.path.stat().st_size
        await self.listener.on_download_complete()

    async def cancel_task(self):
        self.listener.is_cancelled = True
        await self.runner.cancel()
        await self._fail("FFmpeg download cancelled by user.")


async def add_ffmpeg_download(listener, path, max_height=720):
    downloader = FFmpegDownloader(listener, path, max_height)
    gid = token_urlsafe(12)
    add_to_queue, event = await check_running_tasks(listener)
    if add_to_queue:
        async with task_dict_lock:
            task_dict[listener.mid] = QueueStatus(listener, gid, "dl")
        await listener.on_download_start()
        if listener.multi <= 1 and not listener.is_rss:
            await send_status_message(listener.message)
        await event.wait()
        if listener.is_cancelled:
            return
    async with task_dict_lock:
        task_dict[listener.mid] = FFmpegDownloadStatus(listener, downloader, gid)
    if not add_to_queue:
        await listener.on_download_start()
        if listener.multi <= 1 and not listener.is_rss:
            await send_status_message(listener.message)
    await downloader.download()
