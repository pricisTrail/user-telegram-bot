"""Own the web process across startup, settings changes and bot restarts."""

import asyncio
import os
import signal
import sys

from deploy.koyeb_profile import low_memory_enabled


_lock = asyncio.Lock()
_process = None
_port = None
_stop_timeout = 8


def web_server_command(port, values=None):
    if isinstance(port, bool):
        raise ValueError("The web port must be an integer from 1 to 65535.")
    try:
        port = int(port)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("The web port must be an integer from 1 to 65535.") from None
    if not 1 <= port <= 65535:
        raise ValueError("The web port must be an integer from 1 to 65535.")
    if low_memory_enabled(values):
        return [
            sys.executable, "-m", "uvicorn", "web.wserver:app",
            "--host", "0.0.0.0", "--port", str(port), "--workers", "1",
            "--no-access-log",
        ]
    return [
        sys.executable, "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker",
        "-w", "1", "web.wserver:app", "--bind", f"0.0.0.0:{port}",
    ]


def _signal_owned(process, *, kill=False):
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            # The process was placed in its own session at creation. Never use
            # a name-wide pkill that could terminate unrelated web servers.
            os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
        elif kill:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass


async def _reap(process):
    _signal_owned(process)
    try:
        await asyncio.wait_for(process.wait(), _stop_timeout)
    except asyncio.TimeoutError:
        _signal_owned(process, kill=True)
        await process.wait()


async def _finish_despite_cancellation(coroutine):
    task = asyncio.create_task(coroutine)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _stop_unlocked():
    global _process, _port
    if _process is not None:
        try:
            await _finish_despite_cancellation(_reap(_process))
        finally:
            if _process.returncode is not None:
                _process = None
                _port = None


async def start_web_server(port, *, values=None):
    """Start exactly one owned web process, replacing a changed port safely."""
    global _process, _port
    command = web_server_command(port, values)
    port = int(port)
    async with _lock:
        if _process is not None and _process.returncode is None and _port == port:
            return _process
        await _stop_unlocked()
        creation = asyncio.create_task(asyncio.create_subprocess_exec(
            *command, **({"start_new_session": True} if os.name == "posix" else {})
        ))
        try:
            process = await asyncio.shield(creation)
        except asyncio.CancelledError:
            # Spawn can complete after cancellation reaches this coroutine.
            process = await creation
            await _finish_despite_cancellation(_reap(process))
            raise
        _process = process
        _port = port
        return process


async def stop_web_server():
    """Terminate and reap the registered web process, including its workers."""
    async with _lock:
        await _stop_unlocked()
