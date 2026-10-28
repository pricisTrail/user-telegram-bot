"""Exercise upload preparation without Telegram, media tools, or credentials."""

import ast
import logging
from math import isfinite
from os import path as ospath
from pathlib import Path
import re
from time import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock


ROOT = Path(__file__).resolve().parents[2]


class FloodWait(Exception):
    pass


class FloodPremiumWait(Exception):
    pass


class RPCError(Exception):
    pass


class BadRequest(RPCError):
    pass


def load_uploader(namespace):
    """Compile the production class, retaining every method body."""
    path = ROOT / "bot/helper/mirror_leech_utils/upload_utils/telegram_uploader.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definition = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TelegramUploader"
    )
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["TelegramUploader"]


class FFmpegUploadPreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = "/app/downloads/123/video.mp4"
        self.existing = {self.output}
        self.sent = SimpleNamespace(video=object(), document=None)
        self.reply = SimpleNamespace(
            reply_video=AsyncMock(return_value=self.sent),
            reply_document=AsyncMock(return_value=self.sent),
            reply_audio=AsyncMock(return_value=self.sent),
            reply_photo=AsyncMock(return_value=self.sent),
        )
        self.listener = SimpleNamespace(
            thumb="", user_id=456, user_transmission=False, user_dict={},
            is_cancelled=False, as_doc=False, thumbnail_layout="2x2", screen_shots=0,
            skip_generated_thumbnail=True,
            ffmpeg_video_metadata={
                "path": self.output, "width": 1280, "height": 720, "duration": 7200,
            },
            up_dest="", message=self.reply,
        )
        self.document_type = AsyncMock(return_value=(True, True, False))
        self.media_info = AsyncMock(return_value=(7199, None, None))
        self.video_thumb = AsyncMock(return_value=None)
        self.audio_thumb = AsyncMock(return_value=None)
        self.frames_thumb = AsyncMock(return_value=None)
        self.image = MagicMock()
        self.image.open.return_value.__enter__.return_value.size = (320, 180)
        self.remove = AsyncMock()
        self.namespace = {
            "time": time, "ospath": ospath, "Path": Path, "isfinite": isfinite,
            "re_match": re.match, "re_sub": re.sub,
            "LOGGER": logging.getLogger(f"ffmpeg_upload_test.{self.id()}"),
            "Config": SimpleNamespace(
                MEDIA_GROUP=False, LEECH_FILENAME_PREFIX="", FILES_LINKS=False,
            ),
            "TgClient": SimpleNamespace(user=None),
            "Image": self.image,
            "aiopath": SimpleNamespace(
                exists=AsyncMock(side_effect=lambda path: path in self.existing),
                isfile=AsyncMock(side_effect=lambda path: path in self.existing),
            ),
            "get_document_type": self.document_type,
            "get_media_info": self.media_info,
            "get_video_thumbnail": self.video_thumb,
            "get_audio_thumbnail": self.audio_thumb,
            "get_multiple_frames_thumbnail": self.frames_thumb,
            "remove": self.remove, "delete_message": AsyncMock(), "sleep": AsyncMock(),
            "FloodWait": FloodWait, "FloodPremiumWait": FloodPremiumWait,
            "RPCError": RPCError, "BadRequest": BadRequest,
            "retry": lambda **kwargs: lambda function: function,
            "wait_exponential": lambda **kwargs: None,
            "stop_after_attempt": lambda *args: None,
            "retry_if_exception_type": lambda *args: None,
        }
        self.uploader_type = load_uploader(self.namespace)

    async def upload(self):
        uploader = self.uploader_type(self.listener, "/app/downloads/123")
        await uploader._user_settings()
        await uploader._msg_to_reply()
        uploader._up_path = self.output
        await uploader._upload_file("<code>video.mp4</code>", "video.mp4", self.output)
        return uploader

    def assert_no_generated_thumbnails(self):
        self.video_thumb.assert_not_awaited()
        self.audio_thumb.assert_not_awaited()
        self.frames_thumb.assert_not_awaited()

    async def test_low_memory_ffl_reuses_metadata_without_probes_or_generated_thumb(self):
        await self.upload()

        self.document_type.assert_not_awaited()
        self.media_info.assert_not_awaited()
        self.assert_no_generated_thumbnails()
        self.reply.reply_video.assert_awaited_once()
        sent = self.reply.reply_video.await_args.kwargs
        self.assertEqual((sent["width"], sent["height"], sent["duration"]), (1280, 720, 7200))
        self.assertIsNone(sent["thumb"])
        self.assertTrue(sent["supports_streaming"])

    async def test_low_memory_ffl_keeps_saved_user_thumbnail_and_video_dimensions(self):
        self.listener.thumb = "thumbnails/custom.jpg"
        self.existing.add(self.listener.thumb)

        await self.upload()

        self.document_type.assert_not_awaited()
        self.media_info.assert_not_awaited()
        self.assert_no_generated_thumbnails()
        sent = self.reply.reply_video.await_args.kwargs
        self.assertEqual(sent["thumb"], self.listener.thumb)
        self.assertEqual((sent["width"], sent["height"]), (1280, 720))
        self.remove.assert_not_awaited()

    async def test_default_mode_retains_existing_probe_and_thumbnail_behavior(self):
        self.listener.skip_generated_thumbnail = False
        self.listener.thumbnail_layout = ""
        self.video_thumb.return_value = "generated.jpg"
        self.existing.add("generated.jpg")

        await self.upload()

        self.document_type.assert_awaited_once_with(self.output)
        self.media_info.assert_awaited_once_with(self.output)
        self.video_thumb.assert_awaited_once_with(self.output, 7199)
        sent = self.reply.reply_video.await_args.kwargs
        self.assertEqual((sent["width"], sent["height"], sent["duration"]), (320, 180, 7199))
        self.assertEqual(sent["thumb"], "generated.jpg")

    async def test_renamed_or_split_path_cannot_use_original_video_metadata(self):
        self.listener.ffmpeg_video_metadata["path"] = "/app/downloads/123/original.mp4"
        self.listener.thumbnail_layout = ""
        self.video_thumb.return_value = "generated.jpg"

        await self.upload()

        self.document_type.assert_awaited_once_with(self.output)
        self.media_info.assert_awaited_once_with(self.output)
        self.video_thumb.assert_awaited_once_with(self.output, 7199)
        sent = self.reply.reply_video.await_args.kwargs
        self.assertEqual(sent["duration"], 7199)
        self.assertEqual((sent["width"], sent["height"]), (320, 180))

    async def test_other_commands_without_cache_keep_audio_preparation(self):
        del self.listener.skip_generated_thumbnail
        del self.listener.ffmpeg_video_metadata
        self.document_type.return_value = (False, True, False)
        self.media_info.return_value = (60, "Artist", "Title")

        await self.upload()

        self.document_type.assert_awaited_once_with(self.output)
        self.media_info.assert_awaited_once_with(self.output)
        self.audio_thumb.assert_awaited_once_with(self.output)
        sent = self.reply.reply_audio.await_args.kwargs
        self.assertEqual((sent["duration"], sent["performer"], sent["title"]), (60, "Artist", "Title"))
        self.reply.reply_video.assert_not_awaited()

    async def test_document_mode_reuses_ffl_metadata_and_skips_generated_thumbnail(self):
        self.listener.as_doc = True

        await self.upload()

        self.document_type.assert_not_awaited()
        self.media_info.assert_not_awaited()
        self.assert_no_generated_thumbnails()
        self.reply.reply_document.assert_awaited_once()
        self.assertIsNone(self.reply.reply_document.await_args.kwargs["thumb"])
        self.reply.reply_video.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
