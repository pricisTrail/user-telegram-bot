from pathlib import Path
from traceback import extract_tb

from .. import LOGGER, bot_loop
from ..helper.ext_utils.ffmpeg_request import parse_ffmpeg_request
from ..helper.listeners.task_listener import TaskListener
from ..helper.mirror_leech_utils.download_utils.ffmpeg_download import add_ffmpeg_download
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import send_message


def usage():
    command = BotCommands.FfmpegLeechCommand
    return (
        f'<code>/{command} URL -q 720 -n "video.mp4"</code>\n'
        "Or reply to a media URL with the command.\n"
        "Supports finite DASH/HLS and direct media over HTTP(S). "
        "The default quality ceiling is 720p; use <code>-q best</code> for the best track. "
        "Tracks are copied without re-encoding. Use a .mkv filename for other codecs.\n"
        f"Use /{BotCommands.StatusCommand} to view progress or "
        f"/{BotCommands.CancelTaskCommand[0]} by reply to stop."
    )


class FFmpegLeech(TaskListener):
    def __init__(self, client, message):
        self.client = client
        self.message = message
        super().__init__()
        self.same_dir = {}
        self.is_leech = True
        # before_start expects the same empty string supplied by Mirror's parser.
        self.thumb = ""
        self._download_error_reported = False

    async def on_download_error(self, error, button=None):
        if self._download_error_reported:
            return
        self._download_error_reported = True
        await super().on_download_error(error, button)

    async def new_event(self):
        reply = self.message.reply_to_message
        try:
            request = parse_ffmpeg_request(
                self.message.text or self.message.caption,
                (reply.text or reply.caption) if reply else None,
            )
        except ValueError as exc:
            await send_message(self.message, f"{exc}\n\n{usage()}")
            return
        self.link = request.url
        self.name = request.name or f"ffmpeg_{self.mid}.mp4"
        await self.get_tag([])
        stage = "upload setup"
        try:
            await self.before_start()
            stage = "download or upload"
            await add_ffmpeg_download(self, self.dir, request.max_height)
        except Exception as exc:
            frame = extract_tb(exc.__traceback__)[-1]
            # Frame locations help diagnose integration failures without logging
            # exception messages, source lines, locals, or signed media URLs.
            LOGGER.warning(
                "FFmpeg leech job failed (%s; stage=%s; location=%s:%s)",
                type(exc).__name__, stage, Path(frame.filename).name, frame.lineno,
            )
            if stage == "upload setup" and isinstance(exc, ValueError):
                error = (
                    f"Unable to prepare the upload destination. Check /{BotCommands.UserSetCommand[0]} and retry."
                )
            else:
                error = "The bot encountered an internal FFmpeg job error. Please retry later."
            await self.on_download_error(error)


async def ffmpeg_leech(client, message):
    bot_loop.create_task(FFmpegLeech(client, message).new_event())
