"""Exercise the real restart handler without Telegram or native services."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[2]


class RestartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.commands = []
        self.modules = {}

        def module(name, **attributes):
            # Provide real package boundaries for the source's relative imports.
            for count in range(1, len(name.split("."))):
                parent = ".".join(name.split(".")[:count])
                if parent not in self.modules:
                    package = ModuleType(parent)
                    package.__path__ = []
                    self.modules[parent] = package
            result = ModuleType(name)
            result.__dict__.update(attributes)
            self.modules[name] = result
            return result

        async def stop_client():
            self.events.append("stop-client")

        async def send_message(*_):
            return SimpleNamespace(chat=SimpleNamespace(id=1), id=2)

        class FileContext:
            async def __aenter__(inner):
                return SimpleNamespace(write=AsyncMock())

            async def __aexit__(inner, *_):
                return False

        module("aiofiles", open=Mock(return_value=FileContext()))
        module("aiofiles.os", path=SimpleNamespace(isfile=AsyncMock(return_value=False)), remove=AsyncMock())
        module("bot.helper.ext_utils.bot_utils", new_task=lambda function: function)
        self.delete_message = AsyncMock()
        module("bot.helper.telegram_helper.message_utils", send_message=send_message, delete_message=self.delete_message)
        module("bot.helper.ext_utils.db_handler", database=SimpleNamespace())
        module("bot.helper.ext_utils.files_utils", clean_all=AsyncMock())
        module("bot.helper.telegram_helper.button_build", ButtonMaker=Mock())
        self.stop_client = AsyncMock(side_effect=stop_client)
        module("bot.core.telegram_manager", TgClient=SimpleNamespace(stop=self.stop_client))
        module("bot.core.config_manager", Config=SimpleNamespace())
        module("bot.core.jdownloader_booter", jdownloader=SimpleNamespace(is_connected=False))
        module("bot.core.torrent_manager", TorrentManager=SimpleNamespace(close_all=AsyncMock()))
        self.clear_readiness = Mock(side_effect=lambda: self.events.append("clear-readiness"))
        module("deploy.koyeb_environment", clear_readiness=self.clear_readiness)
        self.intervals = {"stopAll": False, "qb": "", "jd": "", "nzb": "", "status": {}}
        self.modules["bot"].__dict__.update(
            intervals=self.intervals,
            scheduler=SimpleNamespace(running=False),
            sabnzbd_client=SimpleNamespace(LOGGED_IN=False),
            LOGGER=SimpleNamespace(error=Mock()),
        )
        patcher = patch.dict(sys.modules, self.modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = importlib.util.spec_from_file_location(
            "bot.modules._koyeb_restart_test", ROOT / "bot" / "modules" / "restart.py"
        )
        self.restart = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.restart)

        async def create_process(*arguments):
            self.commands.append(arguments)
            self.events.append("spawn:" + arguments[0])

            async def wait():
                self.events.append("wait:" + arguments[0])
                return 0

            return SimpleNamespace(wait=AsyncMock(side_effect=wait))

        self.restart.create_subprocess_exec = AsyncMock(side_effect=create_process)
        self.restart.osexecl = Mock(side_effect=lambda *_: self.events.append("exec"))

    def query(self, action="confirm"):
        return SimpleNamespace(
            data=f"botrestart {action}",
            answer=AsyncMock(),
            message=SimpleNamespace(reply_to_message=object()),
        )

    async def test_koyeb_restart_skips_updater_and_reenters_bootstrap(self):
        self.restart.environ = {"MLTB_KOYEB": "true"}
        await self.restart.confirm_restart(None, self.query())

        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][0], "pkill")
        self.restart.osexecl.assert_called_once_with(
            sys.executable, sys.executable, "deploy/koyeb_environment.py"
        )
        self.clear_readiness.assert_called_once_with()
        self.assertLess(self.events.index("clear-readiness"), self.events.index("stop-client"))
        self.assertLess(self.events.index("wait:pkill"), self.events.index("exec"))
        self.assertTrue(self.intervals["stopAll"])

    async def test_default_restart_keeps_updater_and_module_entrypoint(self):
        self.restart.environ = {}
        await self.restart.confirm_restart(None, self.query())

        self.assertEqual(self.commands[0][0], "pkill")
        self.assertEqual(self.commands[1], ("python3", "update.py"))
        self.assertEqual(len(self.commands), 2)
        self.assertIn("wait:pkill", self.events)
        self.assertIn("wait:python3", self.events)
        self.assertLess(self.events.index("wait:python3"), self.events.index("exec"))
        self.restart.osexecl.assert_called_once_with(
            sys.executable, sys.executable, "-m", "bot"
        )
        self.clear_readiness.assert_not_called()

    async def test_false_koyeb_flag_keeps_default_restart(self):
        self.restart.environ = {"MLTB_KOYEB": "false"}
        await self.restart.confirm_restart(None, self.query())
        self.assertIn(("python3", "update.py"), self.commands)
        self.clear_readiness.assert_not_called()
        self.restart.osexecl.assert_called_once_with(
            sys.executable, sys.executable, "-m", "bot"
        )

    async def test_cancel_does_not_clear_readiness_or_restart(self):
        self.restart.environ = {"MLTB_KOYEB": "true"}
        query = self.query("cancel")
        await self.restart.confirm_restart(None, query)

        query.answer.assert_awaited_once_with()
        self.delete_message.assert_awaited_once_with(query.message)
        self.clear_readiness.assert_not_called()
        self.stop_client.assert_not_awaited()
        self.restart.create_subprocess_exec.assert_not_awaited()
        self.restart.osexecl.assert_not_called()
        self.assertFalse(self.intervals["stopAll"])

    async def test_failed_client_shutdown_leaves_readiness_cleared(self):
        self.restart.environ = {"MLTB_KOYEB": "true"}
        self.stop_client.side_effect = RuntimeError("shutdown failed")
        with self.assertRaisesRegex(RuntimeError, "shutdown failed"):
            await self.restart.confirm_restart(None, self.query())
        self.clear_readiness.assert_called_once_with()
        self.restart.create_subprocess_exec.assert_not_awaited()
        self.restart.osexecl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
