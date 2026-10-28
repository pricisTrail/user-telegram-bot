"""Exercise native engine ownership and idle decisions without native tools."""

import ast
import asyncio
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[2]


class FakeProcess:
    def __init__(self, *, ignore_terminate=False, code=None):
        self.returncode = code
        self.ignore_terminate = ignore_terminate
        self.terminated = self.killed = self.waited = False
        self.done = asyncio.Event()
        if code is not None:
            self.done.set()

    def terminate(self):
        self.terminated = True
        if not self.ignore_terminate:
            self.returncode = 0
            self.done.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.done.set()

    async def wait(self):
        self.waited = True
        await self.done.wait()
        return self.returncode


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"MLTB_LOW_MEMORY": "true"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        bot, core = ModuleType("bot"), ModuleType("bot.core")
        bot.__path__, core.__path__ = [], []
        self.modules = patch.dict(sys.modules, {"bot": bot, "bot.core": core})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.bot = bot
        spec = importlib.util.spec_from_file_location("bot.core.engine_manager", ROOT / "bot/core/engine_manager.py")
        self.manager = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.manager
        spec.loader.exec_module(self.manager)
        self.processes = []
        self.spawn = AsyncMock(side_effect=self.spawn_process)
        self.spawn_patch = patch.object(self.manager.asyncio, "create_subprocess_exec", self.spawn)
        self.spawn_patch.start()
        self.addCleanup(self.spawn_patch.stop)
        self.bot.qb_torrents = {}
        self.bot.nzb_jobs = {}
        self.bot.sabnzbd_client = SimpleNamespace(
            LOGGED_IN=False, close=AsyncMock(), get_config=AsyncMock(return_value={}),
            get_downloads=AsyncMock(return_value={"queue": {"slots": []}}),
            get_history=AsyncMock(return_value={"history": {"slots": []}}),
        )
        startup = ModuleType("bot.core.startup")
        startup.update_qb_options = AsyncMock()
        startup.update_nzb_options = AsyncMock()
        sys.modules[startup.__name__] = startup
        self.startup = startup
        self.client = self.new_client()
        self.create_client = AsyncMock(return_value=self.client)
        self.aria = SimpleNamespace(
            getGlobalStat=AsyncMock(return_value={"downloadSpeed": "20", "uploadSpeed": "5"}),
            close=AsyncMock(), forcePauseAll=AsyncMock(), purgeDownloadResult=AsyncMock(),
            tellActive=AsyncMock(return_value=[]), tellWaiting=AsyncMock(return_value=[]),
        )
        namespace = {
            "__name__": "bot.core.torrent_manager", "__package__": "bot.core",
            "Lock": asyncio.Lock, "gather": asyncio.gather, "TimeoutError": asyncio.TimeoutError,
            "Aria2WebsocketClient": SimpleNamespace(new=AsyncMock(return_value=self.aria)),
            "create_client": self.create_client, "wrap_with_retry": lambda client: client,
            "low_memory_enabled": self.manager.low_memory_enabled,
        }
        tree = ast.parse((ROOT / "bot/core/torrent_manager.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TorrentManager")
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "torrent_manager.py", "exec"), namespace)
        self.tm = namespace["TorrentManager"]
        tm_module = ModuleType("bot.core.torrent_manager")
        tm_module.TorrentManager = self.tm
        sys.modules[tm_module.__name__] = tm_module
        self.namespace = namespace

    def new_client(self):
        return SimpleNamespace(
            close=AsyncMock(), torrents=SimpleNamespace(info=AsyncMock(return_value=[]), delete=AsyncMock(), stop=AsyncMock()),
            search=SimpleNamespace(status=AsyncMock(return_value=[])),
            transfer=SimpleNamespace(info=AsyncMock(return_value=SimpleNamespace(dl_info_speed=3, up_info_speed=2))),
        )

    async def spawn_process(self, *args, **kwargs):
        process = FakeProcess()
        self.processes.append(process)
        return process

    async def asyncTearDown(self):
        await self.manager.stop_all()

    async def test_concurrent_start_requests_launch_once_and_wait_for_readiness(self):
        engine = self.manager.EngineProcess("example", lambda: ("example",), retry_interval=0)
        connect = AsyncMock(side_effect=[RuntimeError("synthetic secret"), None])
        await asyncio.gather(*(engine.ensure(connect) for _ in range(8)))
        self.spawn.assert_awaited_once()
        self.assertEqual(connect.await_count, 2)
        self.assertTrue(engine.is_running())
        await engine.stop()
        self.assertTrue(self.processes[0].terminated)
        self.assertTrue(self.processes[0].waited)

    async def test_readiness_timeout_is_safe_and_reaps_native_process(self):
        engine = self.manager.EngineProcess("example", lambda: ("example",), timeout=0.02)
        async def blocked():
            await asyncio.Event().wait()
        with self.assertRaises(self.manager.EngineStartError) as caught:
            await engine.ensure(blocked)
        self.assertNotIn("synthetic", str(caught.exception))
        self.assertTrue(self.processes[0].terminated)
        self.assertTrue(self.processes[0].waited)
        self.assertIsNone(engine.process)

    async def test_cancel_during_os_spawn_still_owns_and_reaps_result(self):
        entered, release = asyncio.Event(), asyncio.Event()
        process = FakeProcess()
        async def spawning(*args, **kwargs):
            entered.set()
            await release.wait()
            return process
        engine = self.manager.EngineProcess("example", lambda: ("example",))
        with patch.object(self.manager.asyncio, "create_subprocess_exec", side_effect=spawning):
            task = asyncio.create_task(engine.ensure(AsyncMock()))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)

    async def test_hung_shutdown_escalates_to_kill(self):
        engine = self.manager.EngineProcess("example", lambda: ("example",), stop_timeout=0.01)
        process = FakeProcess(ignore_terminate=True)
        engine.process = process
        engine.ready = True
        await engine.stop()
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertTrue(process.waited)

    async def test_low_memory_startup_only_connects_aria_and_status_does_not_start_qbit(self):
        await self.tm.initiate()
        self.assertIs(self.tm.aria2, self.aria)
        self.assertIsNone(self.tm.qbittorrent)
        self.create_client.assert_not_awaited()
        self.assertEqual(await self.tm.overall_speed(), (20, 5))
        await self.tm.pause_all()
        await self.tm.remove_all()
        self.spawn.assert_not_awaited()

    async def test_normal_startup_preserves_eager_qbit_client(self):
        with patch.dict(os.environ, {"MLTB_LOW_MEMORY": "false"}):
            await self.tm.initiate()
        self.assertIs(self.tm.qbittorrent, self.client)
        self.create_client.assert_awaited_once()
        self.spawn.assert_not_awaited()

    async def test_qbit_first_command_starts_once_and_replays_configuration(self):
        clients = await asyncio.gather(*(self.tm.ensure_qbit() for _ in range(5)))
        self.assertTrue(all(client is self.client for client in clients))
        self.spawn.assert_awaited_once()
        self.assertNotIn("-d", self.spawn.await_args.args)
        self.create_client.assert_awaited_once()
        self.startup.update_qb_options.assert_awaited_once()
        self.assertTrue(self.manager.engine_running("qbit"))

    async def test_nzb_first_command_starts_once_and_replays_configuration(self):
        await asyncio.gather(*(self.manager.ensure_nzb() for _ in range(5)))
        self.spawn.assert_awaited_once()
        self.assertNotIn("-d", self.spawn.await_args.args)
        self.startup.update_nzb_options.assert_awaited_once()
        self.assertTrue(self.manager.engine_running("nzb"))

    async def test_confirmed_idle_qbit_stops_then_next_command_can_restart(self):
        await self.tm.ensure_qbit()
        await self.manager.stop_idle_engines(now=100)
        await self.manager.stop_idle_engines(now=399)
        self.assertFalse(self.processes[0].terminated)
        await self.manager.stop_idle_engines(now=400)
        self.assertTrue(self.processes[0].terminated)
        self.client.close.assert_awaited_once()
        self.assertIsNone(self.tm.qbittorrent)
        self.create_client.return_value = self.new_client()
        await self.tm.ensure_qbit()
        self.assertEqual(self.spawn.await_count, 2)

    async def test_any_torrent_including_paused_or_seed_prevents_idle_stop(self):
        await self.tm.ensure_qbit()
        self.client.torrents.info.return_value = [SimpleNamespace(state="pausedUP")]
        self.manager._qbit.idle_since = 0
        await self.manager.stop_idle_engines(now=1000)
        self.assertFalse(self.processes[0].terminated)
        self.assertIsNone(self.manager._qbit.idle_since)

    async def test_running_search_and_registry_jobs_prevent_idle_stop(self):
        await self.tm.ensure_qbit()
        self.client.search.status.return_value = [SimpleNamespace(status="Running")]
        self.manager._qbit.idle_since = 0
        await self.manager.stop_idle_engines(now=1000)
        self.assertFalse(self.processes[0].terminated)
        self.bot.qb_torrents["test"] = object()
        self.client.search.status.reset_mock()
        await self.manager.stop_idle_engines(now=2000)
        self.client.search.status.assert_not_awaited()

    async def test_qbit_api_error_always_keeps_engine_alive(self):
        await self.tm.ensure_qbit()
        self.client.torrents.info.side_effect = RuntimeError("synthetic secret")
        self.manager._qbit.idle_since = 0
        await self.manager.stop_idle_engines(now=1000)
        self.assertFalse(self.processes[0].terminated)
        self.assertIsNone(self.manager._qbit.idle_since)

    async def test_status_read_finishes_before_idle_monitor_closes_qbit(self):
        await self.tm.ensure_qbit()
        self.tm.aria2 = self.aria
        entered, release = asyncio.Event(), asyncio.Event()
        async def read_speed():
            entered.set()
            await release.wait()
            self.assertFalse(self.processes[0].terminated)
            self.client.close.assert_not_awaited()
            return SimpleNamespace(dl_info_speed=3, up_info_speed=2)
        self.client.transfer.info.side_effect = read_speed
        self.manager._qbit.idle_since = 0
        status = asyncio.create_task(self.tm.overall_speed())
        await entered.wait()
        stopping = asyncio.create_task(self.manager.stop_idle_engines(now=1000))
        await asyncio.sleep(0)
        self.client.close.assert_not_awaited()
        release.set()
        self.assertEqual(await status, (23, 7))
        await stopping
        self.assertTrue(self.processes[0].terminated)
        self.assertIsNone(self.tm.qbittorrent)

    async def test_confirmed_idle_nzb_stops_and_clears_login(self):
        await self.manager.ensure_nzb()
        self.bot.sabnzbd_client.LOGGED_IN = True
        await self.manager.stop_idle_engines(now=100)
        await self.manager.stop_idle_engines(now=400)
        self.assertFalse(self.bot.sabnzbd_client.LOGGED_IN)
        self.assertTrue(self.processes[0].terminated)
        self.bot.sabnzbd_client.close.assert_awaited_once()

    async def test_nzb_queue_processing_and_registry_jobs_keep_engine_alive(self):
        await self.manager.ensure_nzb()
        for queued, history, registry in (
            ([{"status": "Paused"}], [], {}),
            ([], [{"status": "Extracting"}], {}),
            ([], [], {"test": object()}),
        ):
            self.bot.sabnzbd_client.get_downloads.return_value = {"queue": {"slots": queued}}
            self.bot.sabnzbd_client.get_history.return_value = {"history": {"slots": history}}
            self.bot.nzb_jobs = registry
            self.manager._nzb.idle_since = 0
            await self.manager.stop_idle_engines(now=1000)
            self.assertFalse(self.processes[0].terminated)

    async def test_nzb_api_failure_keeps_engine_alive(self):
        await self.manager.ensure_nzb()
        self.bot.sabnzbd_client.get_downloads.side_effect = RuntimeError("synthetic secret")
        self.manager._nzb.idle_since = 0
        await self.manager.stop_idle_engines(now=1000)
        self.assertFalse(self.processes[0].terminated)
        self.assertIsNone(self.manager._nzb.idle_since)

    async def test_idle_monitor_is_singleton_and_stop_all_cancels_it(self):
        first = self.manager.start_idle_monitor()
        self.assertIs(first, self.manager.start_idle_monitor())
        await self.manager.stop_all()
        self.assertTrue(first.cancelled())
        self.assertIsNone(self.manager._idle_task)


class LauncherTests(unittest.TestCase):
    def test_low_memory_launcher_defers_optional_engines_only(self):
        bash = shutil.which("bash")
        if not bash:
            candidate = Path("F:/installs2/Git/bin/bash.exe")
            bash = str(candidate) if candidate.is_file() else None
        if not bash:
            self.skipTest("Bash unavailable")
        source = (
            "curl() { :; }; aria2c() { printf 'ARIA\\n'; }; "
            "qbittorrent-nox() { printf 'QBIT\\n'; }; "
            "sabnzbdplus() { printf 'SAB\\n'; }; source \"$1\""
        )
        for mode, expected in (("true", ["ARIA"]), ("false", ["ARIA", "QBIT", "SAB"])):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    [bash, "-c", source, "test-launcher", (ROOT / "aria-nox-nzb.sh").as_posix()],
                    env={**os.environ, "MLTB_LOW_MEMORY": mode}, capture_output=True, text=True,
                    timeout=10, check=True,
                )
                self.assertEqual(result.stdout.splitlines(), expected)


if __name__ == "__main__":
    unittest.main()
