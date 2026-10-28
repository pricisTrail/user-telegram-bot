"""Verify owned web process lifecycle and web startup without download engines."""

import ast
import asyncio
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[2]


def load_process_helper():
    spec = importlib.util.spec_from_file_location("web_process_under_test", ROOT / "bot/core/web_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Process:
    def __init__(self, *, ignore_terminate=False):
        self.pid = 12345
        self.returncode = None
        self.ignore_terminate = ignore_terminate
        self.terminated = False
        self.killed = False
        self.waited = False
        self.wait_started = asyncio.Event()
        self.finished = asyncio.Event()

    def terminate(self):
        self.terminated = True
        if not self.ignore_terminate:
            self.finish(0)

    def kill(self):
        self.killed = True
        self.finish(-9)

    def finish(self, code):
        self.returncode = code
        self.finished.set()

    async def wait(self):
        self.waited = True
        self.wait_started.set()
        await self.finished.wait()
        return self.returncode


class WebProcessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_process_helper()
        self.module.os = SimpleNamespace(name="nt")
        self.low = {"MLTB_LOW_MEMORY": "true"}
        self.first, self.second = Process(), Process()
        self.spawn = AsyncMock(side_effect=[self.first, self.second])
        self.module.asyncio = SimpleNamespace(**{
            name: getattr(asyncio, name) for name in (
                "Lock", "create_task", "shield", "CancelledError", "TimeoutError", "wait_for"
            )
        }, create_subprocess_exec=self.spawn)

    async def asyncTearDown(self):
        if self.module._process is not None:
            self.module._process.finish(0)
            await self.module.stop_web_server()

    async def test_low_memory_starts_one_uvicorn_process_without_gunicorn(self):
        await self.module.start_web_server(8080, values=self.low)
        command = self.spawn.await_args.args
        self.assertEqual(command[1:4], ("-m", "uvicorn", "web.wserver:app"))
        self.assertEqual(command[command.index("--workers") + 1], "1")
        self.assertNotIn("gunicorn", command)
        self.assertIn("--no-access-log", command)

    async def test_default_keeps_gunicorn_worker_and_binding(self):
        await self.module.start_web_server(8080, values={})
        command = self.spawn.await_args.args
        self.assertEqual(command[1:3], ("-m", "gunicorn"))
        self.assertIn("uvicorn.workers.UvicornWorker", command)
        self.assertEqual(command[-1], "0.0.0.0:8080")

    async def test_concurrent_starts_same_port_create_only_one_process(self):
        results = await asyncio.gather(*(
            self.module.start_web_server(8080, values=self.low) for _ in range(10)
        ))
        self.spawn.assert_awaited_once()
        self.assertTrue(all(process is self.first for process in results))

    async def test_port_change_reaps_old_process_before_spawning_replacement(self):
        await self.module.start_web_server(8080, values=self.low)

        async def replacement(*args, **kwargs):
            self.assertTrue(self.first.terminated)
            self.assertTrue(self.first.waited)
            self.assertEqual(self.first.returncode, 0)
            return self.second

        self.spawn.side_effect = replacement
        await self.module.start_web_server(8081, values=self.low)
        self.assertIs(self.module._process, self.second)
        self.assertEqual(self.module._port, 8081)

    async def test_invalid_port_preserves_running_process(self):
        await self.module.start_web_server(8080, values=self.low)
        for port in (0, 65536, "invalid", True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                await self.module.start_web_server(port, values=self.low)
        self.assertFalse(self.first.terminated)
        self.spawn.assert_awaited_once()

    async def test_stop_is_idempotent_and_escalates_a_stuck_owned_process(self):
        self.first.ignore_terminate = True
        self.module._stop_timeout = 0.02
        await self.module.start_web_server(8080, values=self.low)
        await self.module.stop_web_server()
        await self.module.stop_web_server()
        self.assertTrue(self.first.terminated)
        self.assertTrue(self.first.killed)
        self.assertTrue(self.first.waited)
        self.assertIsNone(self.module._process)

    async def test_stop_finishes_reaping_even_when_caller_is_cancelled(self):
        self.first.ignore_terminate = True
        await self.module.start_web_server(8080, values=self.low)
        stop = asyncio.create_task(self.module.stop_web_server())
        await self.first.wait_started.wait()
        stop.cancel()
        self.first.finish(0)
        with self.assertRaises(asyncio.CancelledError):
            await stop
        self.assertTrue(self.first.waited)
        self.assertIsNone(self.module._process)

    async def test_cancel_during_spawn_reaps_the_late_created_process(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            return self.first

        self.spawn.side_effect = delayed
        start = asyncio.create_task(self.module.start_web_server(8080, values=self.low))
        await started.wait()
        start.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await start
        self.assertTrue(self.first.terminated)
        self.assertTrue(self.first.waited)
        self.assertIsNone(self.module._process)

    async def test_posix_signals_only_the_registered_process_group(self):
        killpg = Mock()
        self.module.os = SimpleNamespace(name="posix", killpg=killpg)
        self.module.signal = SimpleNamespace(SIGTERM=15, SIGKILL=9)
        self.module._signal_owned(self.first)
        self.module._signal_owned(self.first, kill=True)
        self.assertEqual(killpg.call_args_list[0].args, (self.first.pid, 15))
        self.assertEqual(killpg.call_args_list[1].args, (self.first.pid, 9))


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


class Response:
    def __init__(self, content, status_code=200):
        self.content, self.status_code = content, status_code


class WebWithoutEnginesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.marker = Path(self.directory.name) / "ready"
        self.aria = SimpleNamespace(close=AsyncMock())
        self.qbit = SimpleNamespace(
            close=AsyncMock(), transfer=SimpleNamespace(info=AsyncMock(return_value={})),
            torrents=SimpleNamespace(files=AsyncMock(return_value=[])),
        )
        self.create_client = AsyncMock(return_value=self.qbit)
        self.namespace = {
            "aria2": None, "qbittorrent": None, "qbit_lock": asyncio.Lock(),
            "Aria2HttpClient": Mock(return_value=self.aria), "create_client": self.create_client,
            "sabnzbd_client": SimpleNamespace(LOGGED_IN=False, close=AsyncMock()),
            "asynccontextmanager": asynccontextmanager, "wait_for": asyncio.wait_for,
            "FastAPI": object, "Request": object, "HTTPException": HTTPException,
            "JSONResponse": Response, "Path": Path, "READINESS_FILENAME": self.marker,
            "make_tree": lambda data, engine: {"files": data, "engine": engine},
            "ClientError": RuntimeError, "AQError": RuntimeError,
        }
        path = ROOT / "web/wserver.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        wanted = {"lifespan", "get_qbittorrent", "handle_torrent", "healthcheck"}
        definitions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in wanted]
        for node in definitions:
            if node.name != "lifespan":
                node.decorator_list = []
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), self.namespace)

    async def test_health_and_lifespan_do_not_connect_qbit_without_torrent_requests(self):
        async with self.namespace["lifespan"](object()):
            response = await self.namespace["healthcheck"]()
            self.assertEqual(response.status_code, 503)
            self.marker.write_text("ready")
            response = await self.namespace["healthcheck"]()
            self.assertEqual((response.status_code, response.content), (200, {"status": "ready"}))
            self.create_client.assert_not_awaited()
        self.aria.close.assert_awaited_once()
        self.qbit.close.assert_not_awaited()

    async def test_qbit_selection_connects_lazily_and_closes_during_shutdown(self):
        gid = "1234" + "a" * 28
        request = SimpleNamespace(query_params={"gid": gid, "pin": "1234"}, method="GET")
        async with self.namespace["lifespan"](object()):
            response = await self.namespace["handle_torrent"](request)
            self.assertEqual(response.content, {"files": [], "engine": "qbittorrent"})
            self.qbit.torrents.files.assert_awaited_once_with(gid)
            await self.namespace["get_qbittorrent"]()
            self.create_client.assert_awaited_once()
        self.qbit.close.assert_awaited_once()

    async def test_missing_engine_returns_safe_503_and_drops_stale_client(self):
        self.qbit.transfer.info.side_effect = RuntimeError("synthetic private diagnostic")
        with self.assertRaises(HTTPException) as result:
            await self.namespace["get_qbittorrent"]()
        self.assertEqual(result.exception.status_code, 503)
        self.assertNotIn("synthetic", result.exception.detail)
        self.qbit.close.assert_awaited_once()
        self.assertIsNone(self.namespace["qbittorrent"])

    async def test_cached_session_reconnects_once_after_engine_restart(self):
        stale = SimpleNamespace(
            close=AsyncMock(),
            transfer=SimpleNamespace(info=AsyncMock(side_effect=RuntimeError("stale session"))),
        )
        self.namespace["qbittorrent"] = stale

        result = await self.namespace["get_qbittorrent"]()

        self.assertIs(result, self.qbit)
        stale.close.assert_awaited_once()
        self.create_client.assert_awaited_once_with("http://localhost:8090/api/v2/")
        self.qbit.transfer.info.assert_awaited_once()
        self.qbit.close.assert_not_awaited()


class SettingsLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.client = SimpleNamespace(app=SimpleNamespace(set_preferences=AsyncMock()))
        self.manager = SimpleNamespace(qbittorrent=None)

        async def ensure_qbit():
            self.events.append("qbit-ready")
            self.manager.qbittorrent = self.client

        async def ensure_nzb():
            self.events.append("nzb-ready")

        async def set_nzb_config(*args):
            self.assertIn("nzb-ready", self.events)
            return {"config": {"misc": {args[1]: args[2]}}}

        self.manager.ensure_qbit = AsyncMock(side_effect=ensure_qbit)
        self.start_web = AsyncMock()
        self.stop_web = AsyncMock()
        self.namespace = {
            "new_task": lambda function: function,
            "TorrentManager": self.manager,
            "ensure_nzb": AsyncMock(side_effect=ensure_nzb),
            "sabnzbd_client": SimpleNamespace(set_config=AsyncMock(side_effect=set_nzb_config)),
            "handler_dict": {}, "qbit_options": {}, "nzb_options": {},
            "update_buttons": AsyncMock(), "delete_message": AsyncMock(),
            "database": SimpleNamespace(
                update_qbittorrent=AsyncMock(), update_nzb_config=AsyncMock(), update_config=AsyncMock()
            ),
            "Config": SimpleNamespace(BASE_URL="https://example.test", BASE_URL_PORT=8080, set=Mock()),
            "start_web_server": self.start_web,
            "stop_web_server": self.stop_web,
        }
        path = ROOT / "bot/modules/bot_settings.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        definitions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in {
            "edit_qbit", "edit_nzb", "edit_variable",
        }]
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), self.namespace)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=123), text="2")

    async def test_qbit_edit_starts_engine_before_using_uninitialized_client(self):
        await self.namespace["edit_qbit"](None, self.message, object(), "max_downloads")
        self.manager.ensure_qbit.assert_awaited_once()
        self.client.app.set_preferences.assert_awaited_once_with({"max_downloads": 2})

    async def test_nzb_edit_starts_engine_before_applying_configuration(self):
        await self.namespace["edit_nzb"](None, self.message, object(), "cache_limit")
        self.namespace["ensure_nzb"].assert_awaited_once()
        self.assertEqual(self.namespace["nzb_options"]["cache_limit"], 2)

    async def test_port_edit_uses_owned_server_helper_and_preserves_config_update(self):
        self.message.text = "8081"
        await self.namespace["edit_variable"](None, self.message, object(), "BASE_URL_PORT")
        self.start_web.assert_awaited_once_with(8081)
        self.namespace["Config"].set.assert_called_once_with("BASE_URL_PORT", 8081)
        self.namespace["database"].update_config.assert_awaited_once_with({"BASE_URL_PORT": 8081})

    async def test_enabling_base_url_starts_owned_web_server_on_configured_port(self):
        self.message.text = "https://example.test"
        await self.namespace["edit_variable"](None, self.message, object(), "BASE_URL")
        self.start_web.assert_awaited_once_with(8080)
        self.stop_web.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
