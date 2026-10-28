"""Prepare the Koyeb container environment and replace this process with the bot.

This module intentionally uses only the standard library.  Importing it does not
import the bot, start services, read credentials from files, or open connections.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """A configuration problem whose message is safe to put in startup logs."""


_REQUIRED = ("BOT_TOKEN", "OWNER_ID", "TELEGRAM_API", "TELEGRAM_HASH")
_TRUE = frozenset(("true", "1", "yes", "on"))
_HOST_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\Z")
READINESS_FILENAME = ".koyeb-ready"


def _text(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    if not isinstance(value, str):
        raise ConfigurationError(f"{key} must be a text environment variable.")
    return value.strip()


def _public_base_url(domain: str) -> str:
    # Koyeb supplies a hostname.  Also tolerate a pasted scheme/trailing slash,
    # but never turn credentials, paths, query parameters, or ports into a URL.
    candidate = domain if "://" in domain else f"https://{domain}"
    try:
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").rstrip(".")
        port = parsed.port
        invalid = (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.path not in {"", "/"}
            or "?" in domain
            or "#" in domain
            or any(character.isspace() or ord(character) < 32 for character in domain)
            or len(hostname) > 253
            or not all(_HOST_LABEL.fullmatch(label) for label in hostname.split("."))
        )
    except (ValueError, UnicodeError):
        invalid = True
    if invalid:
        raise ConfigurationError("KOYEB_PUBLIC_DOMAIN must contain only a hostname.")
    return f"https://{hostname.lower()}"


def _validate_explicit_base_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        # Read the port too so malformed ports cannot reach the bot's startup.
        port = parsed.port
        invalid = (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or any(character.isspace() for character in value)
            or any(ord(character) < 32 for character in value)
            or (port is not None and not 1 <= port <= 65535)
        )
    except (ValueError, UnicodeError):
        invalid = True
    if invalid:
        raise ConfigurationError("BASE_URL must be an HTTP(S) URL without credentials, query, or fragment.")


def prepare_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Return validated startup values without modifying the supplied mapping.

    Explicit BASE_URL and BASE_URL_PORT values take priority.  Otherwise use
    Koyeb's public domain and PORT, with 8080 as the final port fallback.
    No exception includes an environment value: tokens, IDs, hashes and URLs
    must not be copied into service logs when configuration is malformed.
    """
    result = dict(env)

    for key in _REQUIRED:
        if not _text(env, key):
            raise ConfigurationError(f"{key} is required.")

    for key in ("OWNER_ID", "TELEGRAM_API"):
        value = _text(env, key)
        try:
            valid_integer = bool(re.fullmatch(r"[0-9]+", value)) and int(value) > 0
        except ValueError:
            valid_integer = False
        if not valid_integer:
            raise ConfigurationError(f"{key} must be a positive integer.")

    base_url = _text(env, "BASE_URL")
    if base_url:
        _validate_explicit_base_url(base_url)
    else:
        domain = _text(env, "KOYEB_PUBLIC_DOMAIN")
        if not domain:
            raise ConfigurationError("Set BASE_URL or provide KOYEB_PUBLIC_DOMAIN so the web server can start.")
        base_url = _public_base_url(domain)
    result["BASE_URL"] = base_url

    source = "BASE_URL_PORT" if _text(env, "BASE_URL_PORT") else "PORT"
    port = _text(env, source) or "8080"
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ConfigurationError(f"{source} must be an integer between 1 and 65535.")
    result["BASE_URL_PORT"] = port

    if _text(env, "MLTB_LOW_MEMORY").lower() in _TRUE:
        for key in ("QUEUE_ALL", "QUEUE_DOWNLOAD", "QUEUE_UPLOAD"):
            if not _text(env, key):
                result[key] = "1"

    result["MLTB_KOYEB"] = "true"
    return result


def apply_platform_config(config: object, env: Mapping[str, str]) -> None:
    """Keep the current platform address after saved Mongo settings are loaded.

    Mongo may contain an address/port from an older deployment.  Only this
    dedicated Koyeb entrypoint marks MLTB_KOYEB, so original Docker/VPS startup
    keeps its existing configuration precedence.  Other settings, including
    database credentials, task options and Telegram identity, are untouched.
    """
    if _text(env, "MLTB_KOYEB").lower() not in _TRUE:
        return
    base_url = _text(env, "BASE_URL")
    if not base_url:
        raise ConfigurationError("BASE_URL must be prepared before applying Koyeb configuration.")
    _validate_explicit_base_url(base_url)
    port = _text(env, "BASE_URL_PORT")
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ConfigurationError("BASE_URL_PORT must be an integer between 1 and 65535.")
    # Match Config's existing URL normalization and integer port type.
    setattr(config, "BASE_URL", base_url.rstrip("/"))
    setattr(config, "BASE_URL_PORT", int(port))


def clear_readiness(app_root: Path | None = None) -> None:
    """Remove only the readiness marker; never recursively delete a directory."""
    ((app_root or Path.cwd()) / READINESS_FILENAME).unlink(missing_ok=True)


def main(env: Mapping[str, str] | None = None, app_root: Path | None = None) -> int:
    """Validate configuration, then exec the bot so container signals reach it."""
    root = app_root or Path.cwd()
    try:
        # Clear before validation too: failed restarts must not retain a stale
        # success marker for a web server still shutting down in the container.
        clear_readiness(root)
    except OSError:
        print("Unable to clear the Koyeb readiness marker.", file=sys.stderr)
        return 1
    try:
        if (root / "config.py").exists():
            raise ConfigurationError(
                "Remove config.py from this Koyeb deployment: it prevents the bot from loading service environment variables."
            )
        prepared = prepare_environment(os.environ if env is None else env)
    except ConfigurationError as error:
        print(f"Koyeb configuration error: {error}", file=sys.stderr)
        return 2

    try:
        os.execvpe(sys.executable, [sys.executable, "-m", "bot"], prepared)
    except OSError:
        print("Unable to start the bot process.", file=sys.stderr)
        return 1
    return 0  # Reachable only when exec is replaced by a test double.


if __name__ == "__main__":
    raise SystemExit(main())
