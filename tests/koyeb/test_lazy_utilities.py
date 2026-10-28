"""Exercise lazy startup utilities without external services or commands."""

import ast
import asyncio
import logging
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[2]


def load_definitions(relative_path, names, namespace):
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definitions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names]
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class LazyVersionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.called = []

        async def run(command, *args):
            self.called.append(command)
            await asyncio.sleep(0)
            return "version 2.3", "", 0

        self.exists = AsyncMock(return_value=False)
        self.namespace = load_definitions("bot/modules/stats.py", {
            "get_version_async", "get_packages_version", "_load_packages_version",
        }, {
            "commands": {
                "one": (["one", "--version"], r"version ([\d.]+)"),
                "two": (["two", "--version"], r"version ([\d.]+)"),
            },
            "_version_lock": asyncio.Lock(), "gather": asyncio.gather,
            "research": re.search, "cmd_exec": run,
            "aiopath": SimpleNamespace(exists=self.exists),
        })

    async def test_versions_do_no_work_until_awaited_and_cache_results(self):
        callback = self.namespace["get_packages_version"]
        self.assertEqual(self.called, [])
        await callback()
        await callback()
        self.assertEqual(self.called, [["one", "--version"], ["two", "--version"]])
        self.assertEqual(self.namespace["commands"], {
            "one": "2.3", "two": "2.3", "commit": "No UPSTREAM_REPO",
        })

    async def test_concurrent_first_requests_collect_each_tool_once(self):
        callback = self.namespace["get_packages_version"]
        await asyncio.gather(callback(), callback(), callback())
        self.assertEqual(len(self.called), 2)
        self.exists.assert_awaited_once_with(".git")

    async def test_cancel_during_commit_lookup_keeps_retryable_command_specs(self):
        entered = asyncio.Event()

        async def wait_for_lookup(path):
            entered.set()
            await asyncio.Event().wait()

        self.exists.side_effect = wait_for_lookup
        task = asyncio.create_task(self.namespace["get_packages_version"]())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsInstance(self.namespace["commands"]["one"], tuple)
        self.exists.side_effect = None
        await self.namespace["get_packages_version"]()
        self.assertEqual(self.namespace["commands"]["one"], "2.3")
        self.assertIn("commit", self.namespace["commands"])


class RetryAfterError(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class LazyTelegraphTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sdk = SimpleNamespace(
            create_account=AsyncMock(return_value={}),
            create_page=AsyncMock(return_value={"path": "result"}),
            edit_page=AsyncMock(return_value=True),
        )
        self.logger = logging.getLogger(f"lazy_telegraph.{self.id()}")
        self.namespace = load_definitions("bot/helper/ext_utils/telegraph_helper.py", {"TelegraphHelper"}, {
            "Telegraph": Mock(return_value=self.sdk), "Lock": asyncio.Lock,
            "LOGGER": self.logger, "token_urlsafe": lambda size: "synthetic-name",
            "RetryAfterError": RetryAfterError, "sleep": AsyncMock(),
        })
        self.helper = self.namespace["TelegraphHelper"]("Author", "https://example.test")

    async def test_first_page_prepares_account_and_later_edits_reuse_it(self):
        self.sdk.create_account.assert_not_awaited()
        result = await self.helper.create_page("Title", "Body")
        await self.helper.edit_page("result", "Title 2", "Body 2")
        self.assertEqual(result, {"path": "result"})
        self.sdk.create_account.assert_awaited_once()
        self.sdk.create_page.assert_awaited_once_with(
            title="Title", author_name="Author", author_url="https://example.test", html_content="Body",
        )
        self.sdk.edit_page.assert_awaited_once()

    async def test_concurrent_first_pages_create_one_account(self):
        async def create_account(**kwargs):
            await asyncio.sleep(0)

        self.sdk.create_account.side_effect = create_account
        await asyncio.gather(self.helper.create_page("One", "A"), self.helper.create_page("Two", "B"))
        self.sdk.create_account.assert_awaited_once()
        self.assertEqual(self.sdk.create_page.await_count, 2)

    async def test_failed_account_creation_is_sanitized_and_can_retry(self):
        marker = "DO_NOT_PRINT_SYNTHETIC_TOKEN"
        self.sdk.create_account.side_effect = [RuntimeError(f"https://example.test/?token={marker}"), {}]
        with self.assertLogs(self.logger, level="ERROR") as captured:
            with self.assertRaises(RuntimeError) as error:
                await self.helper.create_page("Title", "Body")
        self.assertNotIn(marker, str(error.exception))
        self.assertNotIn(marker, " ".join(captured.output))
        self.sdk.create_page.assert_not_awaited()
        self.assertEqual(await self.helper.create_page("Retry", "Body"), {"path": "result"})
        self.assertEqual(self.sdk.create_account.await_count, 2)
        self.sdk.create_page.assert_awaited_once()


class LazySearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.installed = False

        async def ensure():
            self.events.append("ensure")

        async def install(plugins):
            self.events.append("install")
            self.installed = True

        async def start(**kwargs):
            self.assertTrue(self.installed)
            self.events.append("query")
            return SimpleNamespace(id=1)

        self.search_api = SimpleNamespace(
            plugins=AsyncMock(return_value=[SimpleNamespace(name="old")]),
            uninstall_plugin=AsyncMock(), install_plugin=AsyncMock(side_effect=install),
            start=AsyncMock(side_effect=start),
            status=AsyncMock(return_value=[SimpleNamespace(status="Stopped")]),
            results=AsyncMock(return_value=SimpleNamespace(results=[], total=0)),
        )
        self.ensure = AsyncMock(side_effect=ensure)
        self.responses = [{"supported_sites": ["one"]}, {"total": 0, "data": []}]
        self.requests = []

        class Client:
            async def __aenter__(client):
                return client

            async def __aexit__(client, *args):
                pass

            async def get(client, url):
                self.requests.append(url)
                data = self.responses.pop(0)
                return SimpleNamespace(json=lambda: data)

        self.namespace = load_definitions("bot/modules/search.py", {"initiate_search_tools", "_ensure_plugins", "search", "torrent_search_update"}, {
            "_search_initialized": False, "_plugins_initialized": False,
            "_plugin_lock": asyncio.Lock(), "PLUGINS": ["stale"], "SITES": None,
            "TELEGRAPH_LIMIT": 300, "low_memory_enabled": lambda: True,
            "Config": SimpleNamespace(SEARCH_API_LINK="https://api.example.test", SEARCH_PLUGINS=["plugin.py"], SEARCH_LIMIT=5),
            "TorrentManager": SimpleNamespace(ensure_qbit=self.ensure, qbittorrent=SimpleNamespace(search=self.search_api)),
            "AsyncClient": Client, "LOGGER": SimpleNamespace(info=Mock(), error=Mock()),
            "edit_message": AsyncMock(), "new_task": lambda function: function,
            "api_buttons": lambda user_id, method: "buttons",
        })

    async def test_low_memory_api_search_never_starts_qbittorrent(self):
        await self.namespace["initiate_search_tools"]()
        await self.namespace["search"]("query", "one", object(), "apisearch")
        self.ensure.assert_not_awaited()
        self.search_api.plugins.assert_not_awaited()
        self.assertEqual(self.namespace["SITES"], {"one": "One", "all": "All"})
        self.assertTrue(self.namespace["_search_initialized"])
        self.namespace["edit_message"].assert_awaited_once()

    async def test_first_plugin_query_installs_once_before_query_and_reuses_setup(self):
        self.namespace["Config"].SEARCH_API_LINK = ""
        callback = self.namespace["search"]
        await asyncio.gather(callback("first", "one", object(), "plugin"), callback("second", "one", object(), "plugin"))
        self.assertEqual(self.ensure.await_count, 2)
        self.search_api.install_plugin.assert_awaited_once_with(["plugin.py"])
        self.search_api.uninstall_plugin.assert_awaited_once_with(["old"])
        self.assertEqual(self.search_api.start.await_count, 2)
        self.assertLess(self.events.index("install"), self.events.index("query"))
        self.assertTrue(self.namespace["_plugins_initialized"])

    async def test_old_inline_api_button_initializes_sites_before_first_callback(self):
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=123), data="torser 123 one apisearch", answer=AsyncMock(),
            message=SimpleNamespace(reply_to_message=SimpleNamespace(text="/search topic")),
        )
        await self.namespace["torrent_search_update"](None, query)
        self.assertTrue(self.namespace["_search_initialized"])
        self.assertEqual(self.namespace["SITES"]["one"], "One")
        self.assertEqual(len(self.requests), 2)
        self.ensure.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertEqual(self.namespace["edit_message"].await_count, 2)

    async def test_wrong_owner_inline_button_does_not_initialize_integrations(self):
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=456), data="torser 123 one apisearch", answer=AsyncMock(),
            message=SimpleNamespace(reply_to_message=SimpleNamespace(text="/search topic")),
        )
        await self.namespace["torrent_search_update"](None, query)
        query.answer.assert_awaited_once_with("Not Yours!", show_alert=True)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.namespace["_search_initialized"])
        self.ensure.assert_not_awaited()
        self.namespace["edit_message"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
