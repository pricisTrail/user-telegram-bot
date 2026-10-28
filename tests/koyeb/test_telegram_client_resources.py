"""Verify real Telegram client setup using synthetic config and SDK stubs."""

import ast
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "telegram_resources_profile", ROOT / "deploy/koyeb_profile.py"
)
profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile)

NORMAL_OPTIONS = {
    "max_concurrent_transmissions": 10,
    "max_message_cache_size": 15000,
    "max_topic_cache_size": 15000,
}
LOW_MEMORY_OPTIONS = {
    "workers": 2,
    "max_concurrent_transmissions": 1,
    "max_message_cache_size": 500,
    "max_topic_cache_size": 128,
}


def load_client(namespace):
    """Retain the production class without importing Telegram or the bot."""
    path = ROOT / "bot/core/telegram_manager.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definition = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TgClient"
    )
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["TgClient"]


class TelegramClientOptionsTests(unittest.TestCase):
    def test_disabled_profile_preserves_original_limits_and_default_workers(self):
        for values in ({}, {"MLTB_LOW_MEMORY": "false"}, {"MLTB_LOW_MEMORY": "0"}):
            with self.subTest(values=values):
                self.assertEqual(profile.telegram_client_options(values), NORMAL_OPTIONS)

    def test_enabled_profile_limits_uploads_workers_and_both_caches(self):
        self.assertEqual(
            profile.telegram_client_options({"MLTB_LOW_MEMORY": "true"}),
            LOW_MEMORY_OPTIONS,
        )

    def test_options_are_fresh_for_each_client(self):
        values = {"MLTB_LOW_MEMORY": "true"}
        first = profile.telegram_client_options(values)
        first["workers"] = 999
        self.assertEqual(profile.telegram_client_options(values), LOW_MEMORY_OPTIONS)

    def test_runtime_flag_is_read_per_call_and_explicit_empty_values_win(self):
        values = {"MLTB_LOW_MEMORY": "true"}
        with patch.object(profile, "environ", values):
            self.assertEqual(profile.telegram_client_options(), LOW_MEMORY_OPTIONS)
            self.assertEqual(profile.telegram_client_options({}), NORMAL_OPTIONS)
            values["MLTB_LOW_MEMORY"] = "false"
            self.assertEqual(profile.telegram_client_options(), NORMAL_OPTIONS)


class TelegramClientConstructionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sdk_client = SimpleNamespace(
            start=AsyncMock(),
            me=SimpleNamespace(username="synthetic_test_bot", is_premium=True),
        )
        self.constructor = MagicMock(return_value=self.sdk_client)
        self.proxy = {"scheme": "socks5", "hostname": "proxy.example.test", "port": 1080}
        self.config = SimpleNamespace(
            BOT_TOKEN="12345:synthetic-test-token", TELEGRAM_API=12345,
            TELEGRAM_HASH="synthetic-test-hash", TG_PROXY=self.proxy,
            USER_SESSION_STRING="synthetic-test-session",
        )
        self.html_parse_mode = object()
        self.preview = MagicMock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
        self.logger = MagicMock()
        self.client_class = load_client({
            "Lock": asyncio.Lock,
            "Client": self.constructor,
            "Config": self.config,
            "LOGGER": self.logger,
            "enums": SimpleNamespace(ParseMode=SimpleNamespace(HTML=self.html_parse_mode)),
            "LinkPreviewOptions": self.preview,
            "telegram_client_options": profile.telegram_client_options,
        })

    def assert_common_options(self, expected):
        args, kwargs = self.constructor.call_args
        self.assertEqual(args[1:], (self.config.TELEGRAM_API, self.config.TELEGRAM_HASH))
        self.assertIs(kwargs["proxy"], self.proxy)
        self.assertEqual(kwargs["workdir"], "/app")
        self.assertIs(kwargs["parse_mode"], self.html_parse_mode)
        self.assertTrue(kwargs["link_preview_options"].is_disabled)
        for name, value in expected.items():
            self.assertEqual(kwargs[name], value)
        if "workers" not in expected:
            self.assertNotIn("workers", kwargs)
        self.sdk_client.start.assert_awaited_once()
        return args, kwargs

    async def test_bot_uses_low_memory_resources_with_existing_auth_and_options(self):
        with patch.object(profile, "environ", {"MLTB_LOW_MEMORY": "true"}):
            await self.client_class.start_bot()

        args, kwargs = self.assert_common_options(LOW_MEMORY_OPTIONS)
        self.assertEqual(args[0], "12345")
        self.assertEqual(kwargs["bot_token"], self.config.BOT_TOKEN)
        self.assertEqual(kwargs["sleep_threshold"], 0)
        self.assertNotIn("session_string", kwargs)
        self.assertEqual(self.client_class.NAME, "synthetic_test_bot")
        self.assertIs(self.client_class.bot, self.sdk_client)

    async def test_user_uses_low_memory_resources_and_retains_premium_setup(self):
        with patch.object(profile, "environ", {"MLTB_LOW_MEMORY": "true"}):
            await self.client_class.start_user()

        args, kwargs = self.assert_common_options(LOW_MEMORY_OPTIONS)
        self.assertEqual(args[0], "user")
        self.assertEqual(kwargs["session_string"], self.config.USER_SESSION_STRING)
        self.assertEqual(kwargs["sleep_threshold"], 60)
        self.assertNotIn("bot_token", kwargs)
        self.assertTrue(self.client_class.IS_PREMIUM_USER)
        self.assertEqual(self.client_class.MAX_SPLIT_SIZE, 4194304000)
        self.assertIs(self.client_class.user, self.sdk_client)

    async def test_disabled_profile_preserves_bot_constructor_defaults(self):
        self.config.TG_PROXY = {}
        with patch.object(profile, "environ", {}):
            await self.client_class.start_bot()

        kwargs = self.constructor.call_args.kwargs
        self.assertIsNone(kwargs["proxy"])
        for name, value in NORMAL_OPTIONS.items():
            self.assertEqual(kwargs[name], value)
        self.assertNotIn("workers", kwargs)
        self.assertEqual(kwargs["sleep_threshold"], 0)
        self.sdk_client.start.assert_awaited_once()

    async def test_disabled_profile_preserves_user_constructor_defaults(self):
        with patch.object(profile, "environ", {"MLTB_LOW_MEMORY": "false"}):
            await self.client_class.start_user()

        _, kwargs = self.assert_common_options(NORMAL_OPTIONS)
        self.assertEqual(kwargs["session_string"], self.config.USER_SESSION_STRING)
        self.assertEqual(kwargs["sleep_threshold"], 60)

    async def test_absent_user_session_does_not_construct_another_client(self):
        self.config.USER_SESSION_STRING = ""
        with patch.object(profile, "environ", {"MLTB_LOW_MEMORY": "true"}):
            await self.client_class.start_user()

        self.constructor.assert_not_called()
        self.sdk_client.start.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
