"""Opt-in resource limits for small container deployments.

This module deliberately has no application imports. It runs after private
settings have been restored, before any download engine is started.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from os import environ
from pathlib import Path
from typing import Any


QBIT_OPTIONS = {
    "async_io_threads": 2,
    "connection_speed": 4,
    "disk_cache": 16,
    "max_active_checking_torrents": 1,
    "max_active_downloads": 1,
    "max_active_torrents": 1,
    "max_active_uploads": 1,
    "max_connec": 32,
    "max_connec_per_torrent": 16,
    "max_uploads": 4,
    "max_uploads_per_torrent": 2,
    "memory_working_set_limit": 128,
    "queueing_enabled": True,
}

QBIT_SECTIONS = {
    "Application": {"MemoryWorkingSetLimit": "128"},
    "BitTorrent": {
        "Session\\AsyncIOThreadsCount": "2",
        "Session\\ConnectionSpeed": "4",
        "Session\\DiskCacheSize": "16",
        "Session\\MaxActiveCheckingTorrents": "1",
        "Session\\MaxActiveDownloads": "1",
        "Session\\MaxActiveTorrents": "1",
        "Session\\MaxActiveUploads": "1",
        "Session\\MaxConnections": "32",
        "Session\\MaxConnectionsPerTorrent": "16",
        "Session\\MaxUploads": "4",
        "Session\\MaxUploadsPerTorrent": "2",
        "Session\\QueueingSystemEnabled": "true",
    },
}

# Only changeable global/default-download options belong in this dictionary.
# disk-cache and rpc-max-request-size are startup-only switches in the shell
# launcher and must not be sent to aria2.changeGlobalOption().
ARIA2_OPTIONS = {
    "bt-max-peers": "32",
    "max-connection-per-server": "2",
    "max-concurrent-downloads": "1",
    "optimize-concurrent-downloads": "false",
    "split": "2",
}


def low_memory_enabled(values: Mapping[str, str] | None = None) -> bool:
    values = environ if values is None else values
    return values.get("MLTB_LOW_MEMORY", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def telegram_client_options(values: Mapping[str, str] | None = None) -> dict[str, int]:
    """Keep Telegram's workers and caches proportional to the one-task queue."""
    if low_memory_enabled(values):
        return {
            "workers": 2,
            "max_concurrent_transmissions": 1,
            "max_message_cache_size": 500,
            "max_topic_cache_size": 128,
        }
    return {
        "max_concurrent_transmissions": 10,
        "max_message_cache_size": 15000,
        "max_topic_cache_size": 15000,
    }


def patch_ini_section(path: Path, section: str, settings: Mapping[str, str]) -> None:
    """Change just one INI section, preserving SABnzbd's nested-section format."""
    with path.open("r", encoding="utf-8", newline="") as source:
        original = source.read()
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines(keepends=True)
    output: list[str] = []
    pending = dict(settings)
    in_section = False
    found = False

    def append_missing() -> None:
        if pending and output and not output[-1].endswith(("\n", "\r")):
            output[-1] += newline
        output.extend(f"{key}={value}{newline}" for key, value in pending.items())
        pending.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section:
                append_missing()
            in_section = stripped == f"[{section}]"
            found = found or in_section
            output.append(line)
            continue

        if in_section and "=" in line and not stripped.startswith(("#", ";")):
            key = line.split("=", 1)[0].strip()
            if key in settings:
                # All repeated copies receive the same value; an old duplicate
                # cannot silently win after the first copy was replaced.
                output.append(f"{key}={settings[key]}{newline}")
                pending.pop(key, None)
                continue
        output.append(line)

    if in_section:
        append_missing()
    elif not found:
        if output and not output[-1].endswith(("\n", "\r")):
            output[-1] += newline
        output.append(f"[{section}]{newline}")
        append_missing()

    updated = "".join(output)
    if updated != original:
        with path.open("w", encoding="utf-8", newline="") as target:
            target.write(updated)


def apply_low_memory_profile(
    root: str | Path,
    config: Any,
    aria2_options: MutableMapping[str, Any],
    qbit_options: MutableMapping[str, Any],
    values: Mapping[str, str] | None = None,
) -> bool:
    """Apply caps after Mongo restoration; leave other deployments untouched."""
    if not low_memory_enabled(values):
        return False

    root = Path(root)
    qbit_file = root / "qBittorrent" / "config" / "qBittorrent.conf"
    sab_file = root / "sabnzbd" / "SABnzbd.ini"
    # Both files are required by the upstream launcher. Do not replace a
    # missing full profile with a partial file that loses its RPC port/auth.
    if not qbit_file.is_file() or not sab_file.is_file():
        raise FileNotFoundError("Download-engine configuration files are missing")

    for section, settings in QBIT_SECTIONS.items():
        patch_ini_section(qbit_file, section, settings)
    patch_ini_section(sab_file, "misc", {"cache_limit": "16M"})

    # Queue settings and saved engine preferences may have come from Mongo.
    # Apply the caps here rather than before that restoration.
    config.QUEUE_ALL = 1
    config.QUEUE_DOWNLOAD = 1
    config.QUEUE_UPLOAD = 1
    # Empty dictionaries tell upstream to discover each newly started engine's
    # full defaults (including its usual qBittorrent password setup). Preserve
    # that path: it will discover the capped values from the patched INI/CLI.
    # Only restored dictionaries need an explicit overlay before RPC replay.
    if qbit_options:
        qbit_options.update(QBIT_OPTIONS)
    if aria2_options:
        aria2_options.pop("disk-cache", None)
        aria2_options.pop("rpc-max-request-size", None)
        aria2_options.update(ARIA2_OPTIONS)
    return True
