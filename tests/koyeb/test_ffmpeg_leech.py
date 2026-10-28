"""Exercise /ffl's real task setup without Telegram or download services."""

import ast
import asyncio
from collections import Counter
from copy import deepcopy
from html import escape
import importlib.util
import logging
from pathlib import Path
import re
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[2]


def load_class(relative_path, name, namespace):
    """Keep production method bodies while avoiding optional SDK imports."""
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definition = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name
    )
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FFmpegLeechLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.logger = logging.getLogger(f"ffmpeg_leech_test.{self.id()}")
        self.send_message = AsyncMock()
        self.add_download = AsyncMock()
        config = load_class("bot/core/config_manager.py", "Config", {})
        packages = {}
        for name in (
            "bot", "bot.modules", "bot.core", "bot.helper", "bot.helper.ext_utils",
            "bot.helper.listeners", "bot.helper.mirror_leech_utils",
            "bot.helper.mirror_leech_utils.download_utils", "bot.helper.telegram_helper",
        ):
            packages[name] = ModuleType(name)
            packages[name].__path__ = []
        packages["bot"].LOGGER = self.logger
        packages["bot"].bot_loop = SimpleNamespace(create_task=None)
        config_module = ModuleType("bot.core.config_manager")
        config_module.Config = config
        packages[config_module.__name__] = config_module
        self.module_patch = patch.dict(sys.modules, packages)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

        links = load_module(
            "bot.helper.ext_utils.links_utils", "bot/helper/ext_utils/links_utils.py"
        )
        load_module(
            "bot.helper.ext_utils.ffmpeg_request", "bot/helper/ext_utils/ffmpeg_request.py"
        )
        load_module(
            "bot.helper.telegram_helper.bot_commands", "bot/helper/telegram_helper/bot_commands.py"
        )
        namespace = {
            "Config": config,
            "TgClient": SimpleNamespace(user=None, IS_PREMIUM_USER=False),
            "user_data": {},
            "DOWNLOAD_DIR": f"{self.directory.name}/",
            "excluded_extensions": ["aria2", "!qB"],
            "included_extensions": [],
            "new_task": lambda function: function,
            "Counter": Counter,
            "deepcopy": deepcopy,
            "findall": re.findall,
            "is_rclone_path": links.is_rclone_path,
            "is_gdrive_link": links.is_gdrive_link,
            "is_telegram_link": links.is_telegram_link,
            "LOGGER": self.logger,
        }
        self.task_config = load_class("bot/helper/common.py", "TaskConfig", namespace)
        namespace.update({
            "escape": escape,
            "task_dict": {},
            "task_dict_lock": asyncio.Lock(),
            "queue_dict_lock": asyncio.Lock(),
            "queued_dl": {},
            "queued_up": {},
            "non_queued_dl": set(),
            "non_queued_up": set(),
            "intervals": {"status": {}},
            "TorrentManager": SimpleNamespace(
                aria2=SimpleNamespace(purgeDownloadResult=AsyncMock())
            ),
            "gather": asyncio.gather,
            "delete_status": AsyncMock(),
            "send_message": self.send_message,
            "start_from_queued": AsyncMock(),
            "sleep": AsyncMock(),
            "clean_download": AsyncMock(),
        })
        listener = load_class(
            "bot/helper/listeners/task_listener.py", "TaskListener", namespace
        )
        listener_module = ModuleType("bot.helper.listeners.task_listener")
        listener_module.TaskListener = listener
        sys.modules[listener_module.__name__] = listener_module
        download_module = ModuleType(
            "bot.helper.mirror_leech_utils.download_utils.ffmpeg_download"
        )
        download_module.add_ffmpeg_download = self.add_download
        sys.modules[download_module.__name__] = download_module
        message_module = ModuleType("bot.helper.telegram_helper.message_utils")
        message_module.send_message = self.send_message
        sys.modules[message_module.__name__] = message_module
        self.command = load_module("bot.modules.ffmpeg_leech", "bot/modules/ffmpeg_leech.py")
        self.message = SimpleNamespace(
            id=123,
            text="/ffl https://example.org/video.mpd",
            caption=None,
            reply_to_message=None,
            from_user=SimpleNamespace(id=456, username="test_user"),
            sender_chat=None,
            chat=SimpleNamespace(id=456, type=SimpleNamespace(name="PRIVATE")),
        )

    async def test_default_command_runs_real_before_start_then_download_helper(self):
        task = self.command.FFmpegLeech(SimpleNamespace(), self.message)
        self.assertIs(task.before_start.__func__, self.task_config.before_start)

        await task.new_event()

        self.add_download.assert_awaited_once_with(task, task.dir, 720)
        self.send_message.assert_not_awaited()
        self.assertEqual(task.name, "ffmpeg_123.mp4")
        self.assertEqual(task.link, "https://example.org/video.mpd")
        self.assertEqual(task.thumb, "")
        self.assertEqual(task.same_dir, {})
        self.assertTrue(task.is_leech)
        # These values are assigned by the actual shared upload setup.
        self.assertEqual(task.split_size, 2097152000)
        self.assertEqual(task.excluded_extensions, ["aria2", "!qB"])

    async def test_unexpected_error_is_internal_and_never_logs_exception_contents(self):
        marker = "DO_NOT_LOG_SYNTHETIC_TEST_SECRET"
        url = f"https://example.org/video.mpd?token={marker}"
        self.message.text = f"/ffl {url}"
        self.add_download.side_effect = AttributeError(f"failed to handle {url}")
        task = self.command.FFmpegLeech(SimpleNamespace(), self.message)

        with self.assertLogs(self.logger, level="WARNING") as captured:
            await task.new_event()

        self.add_download.assert_awaited_once()
        self.send_message.assert_awaited_once()
        user_message = self.send_message.await_args.args[1]
        self.assertIn("internal FFmpeg job error", user_message)
        self.assertNotIn("usetting", user_message)
        self.assertIn("AttributeError", " ".join(captured.output))
        self.assertIn("location=", " ".join(captured.output))
        for output in [user_message, *captured.output]:
            self.assertNotIn(marker, output)
            self.assertNotIn(url, output)

    async def test_low_memory_flag_only_skips_generated_thumbnail_when_enabled(self):
        for value, expected in (("true", True), (" YES ", True), ("false", False), ("", False)):
            with self.subTest(value=value), patch.dict("os.environ", {"MLTB_LOW_MEMORY": value}):
                task = self.command.FFmpegLeech(SimpleNamespace(), self.message)
                self.assertEqual(task.skip_generated_thumbnail, expected)
                self.assertEqual(task.thumb, "")


if __name__ == "__main__":
    unittest.main()
