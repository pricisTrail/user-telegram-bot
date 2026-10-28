"""Start optional native engines only when a command needs them.

The ordinary deployment keeps its existing eager launcher. Low-memory mode
owns foreground processes so failed startup and shutdown cannot orphan them.
"""

import asyncio
from asyncio.subprocess import DEVNULL
from pathlib import Path
from time import monotonic

from deploy.koyeb_profile import low_memory_enabled


class EngineStartError(RuntimeError):
    """A safe startup error without native diagnostics or credentials."""


class EngineProcess:
    def __init__(self, name, command, *, timeout=30, retry_interval=0.3, stop_timeout=5):
        self.name = name
        self.command = command
        self.timeout = timeout
        self.retry_interval = retry_interval
        self.stop_timeout = stop_timeout
        self.process = None
        self.ready = False
        self.lock = asyncio.Lock()
        self.idle_since = None

    def touch(self):
        self.idle_since = None

    def is_running(self):
        return self.ready and self.process is not None and self.process.returncode is None

    async def _stop_locked(self):
        process, self.process = self.process, None
        self.ready = False
        self.idle_since = None
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), self.stop_timeout)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        await process.wait()

    async def stop(self):
        async with self.lock:
            await self._stop_locked()

    async def _wait_ready(self, connect):
        while True:
            if self.process.returncode is not None:
                raise EngineStartError(f"{self.name} exited before it was ready.")
            try:
                await connect()
                return
            except Exception:
                await asyncio.sleep(self.retry_interval)

    async def ensure(self, connect):
        async with self.lock:
            self.touch()
            if self.is_running():
                return
            await self._stop_locked()
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *self.command(), stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
            ))
            try:
                # Complete an in-flight OS spawn before honoring cancellation,
                # then terminate/reap that process rather than losing its PID.
                try:
                    self.process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    self.process = await spawn
                    raise
                await asyncio.wait_for(self._wait_ready(connect), self.timeout)
                self.ready = True
            except BaseException as error:
                await self._stop_locked()
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise EngineStartError(
                    f"Unable to start {self.name}. Check the service runtime and try again."
                ) from None


_qbit = EngineProcess(
    "qBittorrent", lambda: ("qbittorrent-nox", f"--profile={Path.cwd()}"),
)
_nzb = EngineProcess(
    "SABnzbd", lambda: (
        "sabnzbdplus", "-f", "sabnzbd/SABnzbd.ini", "-s", ":::8070",
        "-b", "0", "-c", "-l", "0", "--console",
    ),
)
_idle_task = None


def engine_running(name):
    if not low_memory_enabled():
        return True
    return {"qbit": _qbit, "nzb": _nzb}[name].is_running()


def touch_engine(name):
    {"qbit": _qbit, "nzb": _nzb}[name].touch()


async def start_qbit(connect):
    await _qbit.ensure(connect)


async def ensure_qbit():
    from .torrent_manager import TorrentManager

    return await TorrentManager.ensure_qbit()


async def ensure_nzb():
    if not low_memory_enabled():
        return

    async def connect():
        from .. import sabnzbd_client
        from .startup import update_nzb_options

        # Ensure the endpoint is ready before replaying/discovering settings.
        await sabnzbd_client.get_config()
        await update_nzb_options()

    await _nzb.ensure(connect)


async def stop_all():
    global _idle_task
    if _idle_task is not None:
        _idle_task.cancel()
        await asyncio.gather(_idle_task, return_exceptions=True)
        _idle_task = None
    await asyncio.gather(_qbit.stop(), _nzb.stop())
    from .. import sabnzbd_client

    sabnzbd_client.LOGGED_IN = False
    await sabnzbd_client.close()


def _idle_elapsed(engine, now, idle_seconds):
    if engine.idle_since is None:
        engine.idle_since = now
    return now - engine.idle_since >= idle_seconds


async def stop_idle_engines(*, idle_seconds=300, now=None):
    """Stop only confirmed-empty engines; an API error always keeps one alive."""
    if not low_memory_enabled():
        return
    from .. import qb_torrents, nzb_jobs, sabnzbd_client
    from .torrent_manager import TorrentManager

    now = monotonic() if now is None else now
    async with TorrentManager._qbit_lock:
        if _qbit.is_running() and TorrentManager.qbittorrent is not None:
            client = TorrentManager.qbittorrent
            try:
                if qb_torrents:
                    _qbit.touch()
                else:
                    torrents, searches = await asyncio.wait_for(asyncio.gather(
                        client.torrents.info(), client.search.status(),
                    ), 5)
                    # Any torrent includes paused downloads and seeds. Retain
                    # unknown search states too instead of guessing they ended.
                    busy = bool(torrents) or any(
                        str(getattr(search, "status", "")).lower() != "stopped"
                        for search in searches
                    )
                    if busy:
                        _qbit.touch()
                    elif _idle_elapsed(_qbit, now, idle_seconds):
                        await client.close()
                        TorrentManager.qbittorrent = None
                        await _qbit.stop()
            except Exception:
                _qbit.touch()

    async with _nzb.lock:
        if _nzb.is_running():
            try:
                if nzb_jobs:
                    _nzb.touch()
                else:
                    queue, history = await asyncio.wait_for(asyncio.gather(
                        sabnzbd_client.get_downloads(), sabnzbd_client.get_history(),
                    ), 5)
                    slots = queue["queue"]["slots"]
                    processing = any(
                        str(job.get("status", "")).lower() not in {"completed", "failed"}
                        for job in history["history"]["slots"]
                    )
                    if slots or processing:
                        _nzb.touch()
                    elif _idle_elapsed(_nzb, now, idle_seconds):
                        sabnzbd_client.LOGGED_IN = False
                        await sabnzbd_client.close()
                        await _nzb._stop_locked()
            except Exception:
                _nzb.touch()


async def _idle_monitor():
    while True:
        await asyncio.sleep(30)
        await stop_idle_engines()


def start_idle_monitor():
    global _idle_task
    if low_memory_enabled() and (_idle_task is None or _idle_task.done()):
        _idle_task = asyncio.create_task(_idle_monitor())
    return _idle_task
