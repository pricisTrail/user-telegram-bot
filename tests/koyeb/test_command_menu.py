import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[2] / "bot/core/command_menu.py"
spec = importlib.util.spec_from_file_location("command_menu_under_test", MODULE_PATH)
menu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(menu)


class DefaultScope:
    pass


class PrivateScope:
    pass


class ChatScope:
    def __init__(self, chat_id):
        self.chat_id = chat_id


class FakeClient:
    def __init__(self, *, mismatch=False, reject=False, error=None):
        self.menus = {}
        self.set_calls = []
        self.get_calls = []
        self.resolved = []
        self.mismatch = mismatch
        self.reject = reject
        self.error = error

    async def get_users(self, user_id):
        self.resolved.append(user_id)

    async def set_bot_commands(self, commands, *, scope, language_code):
        if self.error:
            raise self.error
        self.set_calls.append((scope, language_code))
        self.menus[(type(scope), language_code)] = commands
        return not self.reject

    async def get_bot_commands(self, *, scope, language_code):
        self.get_calls.append((scope, language_code))
        commands = self.menus[(type(scope), language_code)]
        return commands[:-1] if self.mismatch else commands


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        types = ModuleType("pyrogram.types")
        types.BotCommand = lambda command, description: SimpleNamespace(
            command=command, description=description
        )
        types.BotCommandScopeDefault = DefaultScope
        types.BotCommandScopeAllPrivateChats = PrivateScope
        types.BotCommandScopeChat = ChatScope
        self.modules = patch.dict(
            sys.modules, {"pyrogram": ModuleType("pyrogram"), "pyrogram.types": types}
        )
        self.modules.start()
        self.addCleanup(self.modules.stop)

    async def test_overwrites_every_private_chat_precedence_level_and_reads_back(self):
        client = FakeClient()
        self.assertEqual(await menu.install_command_menu(client, 123), 6)
        expected = {
            (scope, language)
            for scope in (DefaultScope, PrivateScope, ChatScope)
            for language in ("", "en")
        }
        self.assertEqual({(type(s), lang) for s, lang in client.set_calls}, expected)
        self.assertEqual({(type(s), lang) for s, lang in client.get_calls}, expected)
        self.assertEqual(client.resolved, [123])
        self.assertEqual(
            [s.chat_id for s, _ in client.set_calls if isinstance(s, ChatScope)],
            [123, 123],
        )

    async def test_suffix_is_published_to_match_registered_handlers(self):
        client = FakeClient()
        await menu.install_command_menu(client, 123, "_test")
        commands = client.menus[(DefaultScope, "")]
        self.assertIn("leech_test", [item.command for item in commands])
        self.assertNotIn("leech", [item.command for item in commands])

    async def test_mismatched_readback_is_not_reported_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "readback"):
            await menu.install_command_menu(FakeClient(mismatch=True), 123)

    async def test_rejected_write_is_not_reported_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            await menu.install_command_menu(FakeClient(reject=True), 123)

    async def test_api_failure_propagates_for_the_startup_boundary(self):
        with self.assertRaisesRegex(OSError, "offline"):
            await menu.install_command_menu(FakeClient(error=OSError("offline")), 123)

    async def test_invalid_suffix_is_rejected_before_api_writes(self):
        for suffix in ("UPPER", "!", "_" * 32):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                await menu.install_command_menu(FakeClient(), 123, suffix)


if __name__ == "__main__":
    unittest.main()
