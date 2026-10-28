"""Keep command registration and FFmpeg tasks independent of optional SDKs."""

import ast
import asyncio
import importlib.util
import inspect
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[2]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lazy = load_module("lazy_callback_test_helper", "bot/core/lazy_callbacks.py")


def load_registry():
    modules = {}
    for name in ("bot", "bot.core", "bot.modules"):
        modules[name] = ModuleType(name)
        modules[name].__path__ = []
    modules["bot.core.lazy_callbacks"] = lazy
    registry = modules["bot.modules"]
    registry.__package__ = "bot.modules"
    with patch.dict(sys.modules, modules):
        exec(compile((ROOT / "bot/modules/__init__.py").read_text(encoding="utf-8"),
                     str(ROOT / "bot/modules/__init__.py"), "exec"), registry.__dict__)
    return registry


class LazyCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_imports_no_command_implementation(self):
        with patch.object(lazy, "import_module", side_effect=AssertionError("Eager feature import")) as importer:
            registry = load_registry()
        importer.assert_not_called()
        self.assertGreater(len(registry.__all__), 45)
        self.assertEqual(set(registry.__all__), set(registry._CALLBACK_MODULES))
        for name in registry.__all__:
            self.assertTrue(inspect.iscoroutinefunction(getattr(registry, name)))
            self.assertEqual(getattr(registry, name).__name__, name)

    async def test_all_real_handlers_register_without_loading_features(self):
        registry = load_registry()
        tree = ast.parse((ROOT / "bot/core/handlers.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "add_handlers")
        registered = []

        class Filter:
            def __and__(self, other):
                return self

        class Commands:
            def __getattr__(self, name):
                return name

        def handler(callback, **kwargs):
            return callback

        namespace = {name: getattr(registry, name) for name in registry.__all__}
        namespace.update({
            "command": lambda *args, **kwargs: Filter(),
            "regex": lambda *args, **kwargs: Filter(),
            "MessageHandler": handler, "CallbackQueryHandler": handler,
            "EditedMessageHandler": handler, "BotCommands": Commands(),
            "CustomFilters": SimpleNamespace(owner=Filter(), sudo=Filter(), authorized=Filter()),
            "TgClient": SimpleNamespace(bot=SimpleNamespace(add_handler=lambda item, **kwargs: registered.append(item))),
        })
        exec(compile(ast.Module(body=[function], type_ignores=[]), "handlers.py", "exec"), namespace)
        with patch.object(lazy, "import_module", side_effect=AssertionError("Eager feature import")):
            namespace["add_handlers"]()
        self.assertGreater(len(registered), 45)
        self.assertTrue(all(inspect.iscoroutinefunction(callback) for callback in registered))

    async def test_first_use_forwards_arguments_and_caches_the_callback(self):
        target = AsyncMock(return_value="result")
        callback = lazy.lazy_async_callback(".feature", "run", package="bot.modules")
        importer = Mock(return_value=SimpleNamespace(run=target))
        client, message = object(), object()
        with patch.object(lazy, "import_module", importer):
            self.assertEqual(await callback(client, message, flag=True), "result")
            self.assertEqual(await callback(client, message, flag=False), "result")
        importer.assert_called_once_with(".feature", "bot.modules")
        self.assertEqual(target.await_args_list[0].args, (client, message))
        self.assertEqual(target.await_args_list[0].kwargs, {"flag": True})
        self.assertEqual(target.await_args_list[1].kwargs, {"flag": False})

    async def test_concurrent_first_calls_load_once(self):
        async def target(value):
            await asyncio.sleep(0)
            return value

        callback = lazy.lazy_async_callback(".feature", "run", package="bot.modules")
        with patch.object(lazy, "import_module", return_value=SimpleNamespace(run=target)) as importer:
            self.assertEqual(await asyncio.gather(callback(1), callback(2)), [1, 2])
        importer.assert_called_once()

    async def test_failed_import_can_be_retried(self):
        target = AsyncMock(return_value="ok")
        callback = lazy.lazy_async_callback(".feature", "run", package="bot.modules")
        with patch.object(lazy, "import_module", side_effect=[ImportError(), SimpleNamespace(run=target)]) as importer:
            with self.assertRaises(ImportError):
                await callback()
            self.assertEqual(await callback(), "ok")
        self.assertEqual(importer.call_count, 2)

    async def test_callback_failure_preserves_loaded_module(self):
        target = AsyncMock(side_effect=[ValueError("synthetic"), "ok"])
        callback = lazy.lazy_async_callback(".feature", "run")
        with patch.object(lazy, "import_module", return_value=SimpleNamespace(run=target)) as importer:
            with self.assertRaises(ValueError):
                await callback()
            self.assertEqual(await callback(), "ok")
        importer.assert_called_once()

    async def test_new_task_callbacks_keep_their_scheduled_task_return_value(self):
        gate = asyncio.Event()

        async def implementation():
            await gate.wait()
            return "finished"

        async def scheduled_callback():
            return asyncio.create_task(implementation())

        callback = lazy.lazy_async_callback(".feature", "run")
        with patch.object(lazy, "import_module", return_value=SimpleNamespace(run=scheduled_callback)):
            task = await asyncio.wait_for(callback(), 1)
        self.assertIsInstance(task, asyncio.Task)
        self.assertFalse(task.done())
        gate.set()
        self.assertEqual(await task, "finished")


class LazyDependencyTests(unittest.IsolatedAsyncioTestCase):
    def load_duplicate_check(self):
        path = ROOT / "bot/helper/ext_utils/task_manager.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "stop_duplicate_check")
        namespace = {
            "__package__": "bot.helper.ext_utils", "is_gdrive_id": lambda value: value == "drive-target",
            "LOGGER": SimpleNamespace(info=Mock()), "get_base_name": lambda name: name,
            "sync_to_async": AsyncMock(return_value=([], 0)), "get_telegraph_list": AsyncMock(return_value="button"),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        return namespace

    async def test_leech_duplicate_check_never_loads_drive_sdk(self):
        namespace = self.load_duplicate_check()
        listener = SimpleNamespace(is_leech=True)
        self.assertEqual(await namespace["stop_duplicate_check"](listener), (False, None))
        namespace["sync_to_async"].assert_not_awaited()

    async def test_drive_duplicate_check_loads_search_only_when_needed(self):
        namespace = self.load_duplicate_check()
        search = Mock(return_value=SimpleNamespace(drive_list=object()))
        module = ModuleType("bot.helper.mirror_leech_utils.gdrive_utils.search")
        module.GoogleDriveSearch = search
        namespace["sync_to_async"].return_value = (["result"], 1)
        listener = SimpleNamespace(is_leech=False, stop_duplicate=True, same_dir={}, select=False,
                                   up_dest="drive-target", name="file", compress=False, extract=False,
                                   is_clone=False, user_id=123)
        with patch.dict(sys.modules, {module.__name__: module}):
            message, button = await namespace["stop_duplicate_check"](listener)
        search.assert_called_once_with(stop_dup=True, no_multi=False)
        self.assertIn("already available", message)
        self.assertEqual(button, "button")
        namespace["sync_to_async"].assert_awaited_once()
        namespace["get_telegraph_list"].assert_awaited_once_with(["result"])

    async def test_ffmpeg_cold_import_graph_excludes_optional_sdks(self):
        visited, external = set(), set()

        def visit(module):
            if module in visited:
                return
            visited.add(module)
            path = ROOT.joinpath(*module.split(".")).with_suffix(".py")
            if not path.is_file():
                path = ROOT.joinpath(*module.split("."), "__init__.py")
            if not path.is_file():
                return
            package = module if path.name == "__init__.py" else module.rpartition(".")[0]
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        prefix = package.split(".")
                        prefix = prefix[:len(prefix) - node.level + 1]
                        target = ".".join(prefix + ([node.module] if node.module else []))
                    else:
                        target = node.module or ""
                    if target.startswith(("bot", "deploy")):
                        visit(target)
                    else:
                        external.add(target.split(".")[0])
                elif isinstance(node, ast.Import):
                    external.update(item.name.split(".")[0] for item in node.names)

        visit("bot.modules.ffmpeg_leech")
        self.assertFalse(external & {
            "google", "googleapiclient", "google_auth_httplib2", "yt_dlp", "gallery_dl", "myjd", "telegraph", "requests",
        })


if __name__ == "__main__":
    unittest.main()
