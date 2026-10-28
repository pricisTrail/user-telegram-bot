"""Run the real startup coroutine with synthetic services and no credentials."""

import ast
import builtins
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[2]


class LazyStartupTests(unittest.IsolatedAsyncioTestCase):
    async def run_startup(self, *, low=True, jd=False, rclone=False, rss=False):
        calls = {}
        modules = {}

        def module(name, **attrs):
            result = ModuleType(name)
            result.__path__ = []
            result.__dict__.update(attrs)
            modules[name] = result
            return result

        def service(name):
            calls[name] = AsyncMock()
            return calls[name]

        package = "memory_startup_test"
        module(package, rss_dict={1: {"synthetic": True}} if rss else {})
        for part in ("core", "helper", "helper.ext_utils", "helper.mirror_leech_utils", "helper.mirror_leech_utils.rclone_utils"):
            module(f"{package}.{part}")
        names = (
            "load_settings", "load_configurations", "save_settings", "update_qb_options",
            "update_aria2_options", "update_nzb_options", "update_variables",
        )
        module(f"{package}.core.startup", **{name: service(name) for name in names})
        module(f"{package}.core.torrent_manager", TorrentManager=SimpleNamespace(initiate=service("torrent_initiate")))
        module(f"{package}.helper.ext_utils.files_utils", clean_all=service("clean_all"))
        module(f"{package}.core.jdownloader_booter", jdownloader=SimpleNamespace(boot=service("jd_boot")))
        module(f"{package}.helper.ext_utils.telegraph_helper", telegraph=SimpleNamespace(create_account=service("telegraph")))
        module(f"{package}.helper.mirror_leech_utils.rclone_utils.serve", rclone_serve_booter=service("rclone"))
        module(f"{package}.modules", **{
            "initiate_search_tools": service("search"),
            "get_packages_version": service("versions"),
            "restart_notification": service("notification"),
            "rss": SimpleNamespace(),
        })
        module(f"{package}.core.command_menu", install_command_menu=service("menu"))
        # The optional engine monitor is a no-op service in startup tests.
        module(f"{package}.core.engine_manager", start_idle_monitor=lambda: calls.setdefault("idle_monitor", True))
        path = ROOT / "bot/__main__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        definition = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "main")
        namespace = {
            "__package__": package,
            "Config": SimpleNamespace(JD_EMAIL="synthetic" if jd else "", JD_PASS="synthetic" if jd else "", RCLONE_SERVE_URL="https://example.test" if rclone else "", OWNER_ID=1, CMD_SUFFIX=""),
            "TgClient": SimpleNamespace(start_bot=service("bot"), start_user=service("user"), bot=object()),
            "KOYEB_MODE": False, "low_memory_enabled": lambda: low,
            "LOGGER": SimpleNamespace(info=lambda *args: None, warning=lambda *args: None),
        }
        exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), namespace)
        imports = []
        original_import = builtins.__import__

        def record_import(name, globals=None, locals=None, fromlist=(), level=0):
            imports.append((name, tuple(fromlist or ())))
            return original_import(name, globals, locals, fromlist, level)

        with patch.dict(sys.modules, modules), patch("builtins.__import__", side_effect=record_import):
            await namespace["main"]()
        return calls, imports

    async def test_low_memory_boot_defers_unconfigured_engines_and_integrations(self):
        calls, imports = await self.run_startup()
        for name in ("update_qb_options", "update_nzb_options", "search", "versions", "telegraph", "jd_boot", "rclone"):
            calls[name].assert_not_awaited()
        for name in ("update_aria2_options", "clean_all", "notification", "menu", "torrent_initiate", "bot", "user"):
            calls[name].assert_awaited_once()
        self.assertFalse(any("jdownloader_booter" in name or "telegraph_helper" in name or "rclone_utils.serve" in name for name, _ in imports))

    async def test_configured_optional_background_services_and_rss_are_preserved(self):
        calls, imports = await self.run_startup(jd=True, rclone=True, rss=True)
        calls["jd_boot"].assert_awaited_once()
        calls["rclone"].assert_awaited_once()
        self.assertTrue(any(name == "modules" and "rss" in attrs for name, attrs in imports))
        calls["search"].assert_not_awaited()

    async def test_default_profile_keeps_eager_initialization(self):
        calls, _ = await self.run_startup(low=False)
        for name in ("update_qb_options", "update_nzb_options", "search", "versions", "telegraph", "jd_boot", "rclone"):
            calls[name].assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
