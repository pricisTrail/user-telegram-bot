"""Isolated startup configuration tests; no bot dependencies or credentials."""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "koyeb_environment", _ROOT / "deploy" / "koyeb_environment.py"
)
environment = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(environment)


def valid_env(**updates: str) -> dict[str, str]:
    # Test-only placeholders; never use live Telegram or database credentials.
    return {
        "BOT_TOKEN": "test-token",
        "OWNER_ID": "1",
        "TELEGRAM_API": "1",
        "TELEGRAM_HASH": "test-hash",
        "KOYEB_PUBLIC_DOMAIN": "mirror-test.koyeb.app",
        **updates,
    }


class PrepareEnvironmentTests(unittest.TestCase):
    def test_uses_koyeb_domain_and_port(self):
        result = environment.prepare_environment(valid_env(PORT="9090"))
        self.assertEqual(result["BASE_URL"], "https://mirror-test.koyeb.app")
        self.assertEqual(result["BASE_URL_PORT"], "9090")

    def test_default_port_is_8080(self):
        result = environment.prepare_environment(valid_env())
        self.assertEqual(result["BASE_URL_PORT"], "8080")

    def test_explicit_url_and_port_take_priority(self):
        result = environment.prepare_environment(
            valid_env(BASE_URL="https://custom.example/base/", BASE_URL_PORT="8123", PORT="not-used")
        )
        self.assertEqual(result["BASE_URL"], "https://custom.example/base/")
        self.assertEqual(result["BASE_URL_PORT"], "8123")

    def test_empty_values_use_fallbacks(self):
        result = environment.prepare_environment(valid_env(BASE_URL="  ", BASE_URL_PORT=" ", PORT=""))
        self.assertEqual(result["BASE_URL"], "https://mirror-test.koyeb.app")
        self.assertEqual(result["BASE_URL_PORT"], "8080")

    def test_domain_normalizes_pasted_scheme_and_slash(self):
        for domain in ("MIRROR-TEST.koyeb.app", "https://MIRROR-TEST.koyeb.app/", "http://mirror-test.koyeb.app/"):
            with self.subTest(domain=domain):
                result = environment.prepare_environment(valid_env(KOYEB_PUBLIC_DOMAIN=domain))
                self.assertEqual(result["BASE_URL"], "https://mirror-test.koyeb.app")

    def test_invalid_domain_is_rejected_without_echoing_value(self):
        invalid = (
            "https://user:private-password@host.example",
            "https://host.example:443",
            "https://host.example/private-path",
            "https://host.example?secret=private-value",
            "https://host.example#private-value",
            "host.example?",
            "host.example#",
            "host name.example",
            "host\n.example",
            "-invalid.example",
            "ftp://host.example",
            "https://[invalid",
        )
        for domain in invalid:
            with self.subTest(domain=domain):
                with self.assertRaises(environment.ConfigurationError) as caught:
                    environment.prepare_environment(valid_env(KOYEB_PUBLIC_DOMAIN=domain))
                self.assertNotIn(domain, str(caught.exception))
                self.assertNotIn("private-password", str(caught.exception))
                self.assertIn("KOYEB_PUBLIC_DOMAIN", str(caught.exception))

    def test_explicit_url_does_not_require_public_domain(self):
        env = valid_env(BASE_URL="https://custom.example")
        del env["KOYEB_PUBLIC_DOMAIN"]
        self.assertEqual(environment.prepare_environment(env)["BASE_URL"], "https://custom.example")

    def test_explicit_url_rejects_credentials_and_unusable_urls(self):
        for value in ("custom.example", "ftp://host.example", "https://user:private-password@host.example", "https://host.example?token=private-value", "https://host.example#fragment", "https://host.example?", "https://host.example#", "https://host.example:bad", "https://host.example:0"):
            with self.subTest(value=value):
                with self.assertRaises(environment.ConfigurationError) as caught:
                    environment.prepare_environment(valid_env(BASE_URL=value))
                self.assertNotIn(value, str(caught.exception))
                self.assertIn("BASE_URL", str(caught.exception))

    def test_missing_public_address_fails_before_startup(self):
        env = valid_env()
        del env["KOYEB_PUBLIC_DOMAIN"]
        with self.assertRaisesRegex(environment.ConfigurationError, "BASE_URL"):
            environment.prepare_environment(env)

    def test_invalid_ports_are_not_echoed(self):
        for key in ("BASE_URL_PORT", "PORT"):
            for port in ("0", "65536", "-1", "+8080", "8.0", "1e3", "private-value", "８０８０", "80\n80", "9" * 5000):
                with self.subTest(key=key, port=port):
                    with self.assertRaises(environment.ConfigurationError) as caught:
                        environment.prepare_environment(valid_env(**{key: port}))
                    self.assertIn(key, str(caught.exception))
                    self.assertNotIn("private-value", str(caught.exception))

    def test_valid_port_boundaries(self):
        for port in ("1", "65535"):
            self.assertEqual(environment.prepare_environment(valid_env(PORT=port))["BASE_URL_PORT"], port)

    def test_required_fields_fail_with_names_only(self):
        for key in ("BOT_TOKEN", "OWNER_ID", "TELEGRAM_API", "TELEGRAM_HASH"):
            with self.subTest(key=key):
                env = valid_env()
                env[key] = "  "
                with self.assertRaisesRegex(environment.ConfigurationError, key):
                    environment.prepare_environment(env)

    def test_invalid_numeric_credentials_do_not_leak(self):
        for key in ("OWNER_ID", "TELEGRAM_API"):
            for value in ("0", "-1", "private-value", "1.5", "9" * 5000):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(environment.ConfigurationError) as caught:
                        environment.prepare_environment(valid_env(**{key: value}))
                    self.assertIn(key, str(caught.exception))
                    self.assertNotIn("private-value", str(caught.exception))

    def test_low_memory_defaults_only_when_enabled(self):
        for mode in ("true", "1", "YES", "on"):
            result = environment.prepare_environment(valid_env(MLTB_LOW_MEMORY=mode))
            self.assertEqual([result[key] for key in ("QUEUE_ALL", "QUEUE_DOWNLOAD", "QUEUE_UPLOAD")], ["1", "1", "1"])
        for mode in ("", "false", "0"):
            result = environment.prepare_environment(valid_env(MLTB_LOW_MEMORY=mode))
            self.assertNotIn("QUEUE_ALL", result)

    def test_explicit_queue_is_preserved_for_runtime_profile(self):
        result = environment.prepare_environment(valid_env(MLTB_LOW_MEMORY="true", QUEUE_ALL="2"))
        self.assertEqual(result["QUEUE_ALL"], "2")

    def test_preserves_unrelated_values_and_input_mapping(self):
        env = valid_env(DATABASE_URL="private-database-placeholder", CUSTOM_SETTING="leave-me")
        before = env.copy()
        result = environment.prepare_environment(env)
        self.assertEqual(env, before)
        self.assertIsNot(result, env)
        self.assertEqual(result["DATABASE_URL"], env["DATABASE_URL"])
        self.assertEqual(result["CUSTOM_SETTING"], env["CUSTOM_SETTING"])

    def test_error_does_not_partially_mutate_input(self):
        env = valid_env(PORT="invalid")
        before = env.copy()
        with self.assertRaises(environment.ConfigurationError):
            environment.prepare_environment(env)
        self.assertEqual(env, before)


class PlatformConfigurationTests(unittest.TestCase):
    def test_preparation_marks_koyeb_environment(self):
        prepared = environment.prepare_environment(valid_env(MLTB_KOYEB="false"))
        self.assertEqual(prepared["MLTB_KOYEB"], "true")

    def test_current_url_and_port_override_saved_settings_only(self):
        prepared = environment.prepare_environment(valid_env(PORT="9090"))
        config = SimpleNamespace(
            BASE_URL="https://retired.example",
            BASE_URL_PORT=80,
            DATABASE_URL="private-database-placeholder",
            OWNER_ID=42,
            QUEUE_ALL=2,
        )
        previous = vars(config).copy()
        environment.apply_platform_config(config, prepared)
        self.assertEqual(config.BASE_URL, "https://mirror-test.koyeb.app")
        self.assertEqual(config.BASE_URL_PORT, 9090)
        for key in ("DATABASE_URL", "OWNER_ID", "QUEUE_ALL"):
            self.assertEqual(getattr(config, key), previous[key])

    def test_explicit_current_address_keeps_existing_normalization(self):
        prepared = environment.prepare_environment(
            valid_env(BASE_URL="https://custom.example/base/", BASE_URL_PORT="8123")
        )
        config = SimpleNamespace(BASE_URL="https://retired.example", BASE_URL_PORT=80)
        environment.apply_platform_config(config, prepared)
        self.assertEqual(config.BASE_URL, "https://custom.example/base")
        self.assertEqual(config.BASE_URL_PORT, 8123)

    def test_original_startup_keeps_saved_configuration(self):
        for marker in (None, "", "false", "0"):
            with self.subTest(marker=marker):
                env = {} if marker is None else {"MLTB_KOYEB": marker}
                config = SimpleNamespace(BASE_URL="https://saved.example", BASE_URL_PORT=80)
                previous = vars(config).copy()
                environment.apply_platform_config(config, env)
                self.assertEqual(vars(config), previous)

    def test_invalid_platform_address_does_not_partially_update(self):
        for updates in (
            {"BASE_URL": ""},
            {"BASE_URL": "https://user:private-value@host.example"},
            {"BASE_URL_PORT": "private-value"},
            {"BASE_URL_PORT": "0"},
        ):
            with self.subTest(updates=updates):
                env = {"MLTB_KOYEB": "true", "BASE_URL": "https://current.example", "BASE_URL_PORT": "8080", **updates}
                config = SimpleNamespace(BASE_URL="https://saved.example", BASE_URL_PORT=80)
                previous = vars(config).copy()
                with self.assertRaises(environment.ConfigurationError) as caught:
                    environment.apply_platform_config(config, env)
                self.assertEqual(vars(config), previous)
                self.assertNotIn("private-value", str(caught.exception))


class EntrypointTests(unittest.TestCase):
    def test_execs_bot_with_prepared_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(environment.os, "execvpe") as execute:
                self.assertEqual(environment.main(valid_env(PORT="9090"), Path(directory)), 0)
            command, arguments, child_env = execute.call_args.args
            self.assertEqual(command, sys.executable)
            self.assertEqual(arguments, [sys.executable, "-m", "bot"])
            self.assertEqual(child_env["BASE_URL_PORT"], "9090")
            self.assertEqual(child_env["MLTB_KOYEB"], "true")

    def test_clears_stale_readiness_before_exec(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / environment.READINESS_FILENAME
            marker.write_text("ready\n")
            unrelated = root / "keep.txt"
            unrelated.write_text("keep\n")

            def execute(*_):
                self.assertFalse(marker.exists())

            with patch.object(environment.os, "execvpe", side_effect=execute):
                self.assertEqual(environment.main(valid_env(), root), 0)
            self.assertTrue(unrelated.exists())

    def test_clears_readiness_even_when_configuration_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / environment.READINESS_FILENAME
            marker.write_text("ready\n")
            with redirect_stderr(io.StringIO()), patch.object(environment.os, "execvpe") as execute:
                self.assertEqual(environment.main(valid_env(OWNER_ID="invalid"), root), 2)
            self.assertFalse(marker.exists())
            execute.assert_not_called()

    def test_missing_readiness_marker_is_harmless(self):
        with tempfile.TemporaryDirectory() as directory:
            environment.clear_readiness(Path(directory))

    def test_readiness_directory_is_not_recursively_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / environment.READINESS_FILENAME
            marker.mkdir()
            (marker / "keep.txt").write_text("keep\n")
            errors = io.StringIO()
            with redirect_stderr(errors), patch.object(environment.os, "execvpe") as execute:
                self.assertEqual(environment.main(valid_env(), root), 1)
            self.assertTrue((marker / "keep.txt").exists())
            self.assertIn("readiness", errors.getvalue())
            execute.assert_not_called()

    def test_readiness_io_failure_does_not_leak_details(self):
        errors = io.StringIO()
        with redirect_stderr(errors), patch.object(environment, "clear_readiness", side_effect=OSError("private-value")), patch.object(environment.os, "execvpe") as execute:
            self.assertEqual(environment.main(valid_env()), 1)
        self.assertNotIn("private-value", errors.getvalue())
        execute.assert_not_called()

    def test_private_config_module_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.py").write_text("# private configuration\n")
            errors = io.StringIO()
            with redirect_stderr(errors), patch.object(environment.os, "execvpe") as execute:
                result = environment.main(valid_env(), Path(directory))
            self.assertEqual(result, 2)
            self.assertIn("config.py", errors.getvalue())
            self.assertIn("environment variables", errors.getvalue())
            execute.assert_not_called()

    def test_validation_error_does_not_start_bot_or_print_values(self):
        with tempfile.TemporaryDirectory() as directory:
            errors = io.StringIO()
            with redirect_stderr(errors), patch.object(environment.os, "execvpe") as execute:
                result = environment.main(valid_env(OWNER_ID="private-value"), Path(directory))
            self.assertEqual(result, 2)
            self.assertNotIn("private-value", errors.getvalue())
            execute.assert_not_called()

    def test_exec_failure_is_reported_without_environment_values(self):
        with tempfile.TemporaryDirectory() as directory:
            errors = io.StringIO()
            with redirect_stderr(errors), patch.object(environment.os, "execvpe", side_effect=OSError("private-value")):
                result = environment.main(valid_env(), Path(directory))
            self.assertEqual(result, 1)
            self.assertNotIn("private-value", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
