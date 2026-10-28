from aioaria2 import Aria2WebsocketClient
from aioqbt.client import create_client
from asyncio import gather, TimeoutError, Lock
from aiohttp import ClientError
from pathlib import Path
from inspect import iscoroutinefunction
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from .. import LOGGER, aria2_options
from deploy.koyeb_profile import low_memory_enabled


def wrap_with_retry(obj, max_retries=3):
    for attr_name in dir(obj):
        if attr_name.startswith("_"):
            continue

        attr = getattr(obj, attr_name)
        if iscoroutinefunction(attr):
            retry_policy = retry(
                stop=stop_after_attempt(max_retries),
                wait=wait_exponential(multiplier=1, min=1, max=5),
                retry=retry_if_exception_type(
                    (ClientError, TimeoutError, RuntimeError)
                ),
            )
            wrapped = retry_policy(attr)
            setattr(obj, attr_name, wrapped)
    return obj


class TorrentManager:
    aria2 = None
    qbittorrent = None
    _qbit_lock = Lock()

    @classmethod
    async def initiate(cls):
        if low_memory_enabled():
            cls.aria2 = await Aria2WebsocketClient.new("http://localhost:6800/jsonrpc")
            return
        cls.aria2, cls.qbittorrent = await gather(
            Aria2WebsocketClient.new("http://localhost:6800/jsonrpc"),
            create_client("http://localhost:8090/api/v2/"),
        )
        cls.qbittorrent = wrap_with_retry(cls.qbittorrent)

    @classmethod
    async def ensure_qbit(cls):
        from .engine_manager import engine_running, start_qbit, touch_engine

        async with cls._qbit_lock:
            touch_engine("qbit")
            if cls.qbittorrent is not None and engine_running("qbit"):
                return cls.qbittorrent

            async def connect():
                if cls.qbittorrent is not None:
                    await cls.qbittorrent.close()
                    cls.qbittorrent = None
                client = await create_client("http://localhost:8090/api/v2/")
                cls.qbittorrent = wrap_with_retry(client)
                try:
                    from .startup import update_qb_options

                    await update_qb_options()
                except BaseException:
                    cls.qbittorrent = None
                    await client.close()
                    raise

            if low_memory_enabled():
                await start_qbit(connect)
            else:
                await connect()
            return cls.qbittorrent

    @classmethod
    async def close_all(cls):
        async with cls._qbit_lock:
            await gather(*(client.close() for client in (cls.aria2, cls.qbittorrent) if client is not None))
            cls.aria2 = cls.qbittorrent = None
            if low_memory_enabled():
                from .engine_manager import stop_all

                await stop_all()

    @classmethod
    async def aria2_remove(cls, download):
        if download.get("status", "") in ["active", "paused", "waiting"]:
            await cls.aria2.forceRemove(download.get("gid", ""))
        else:
            try:
                await cls.aria2.removeDownloadResult(download.get("gid", ""))
            except:
                pass

    @classmethod
    async def remove_all(cls):
        await cls.pause_all()
        operations = [cls.aria2.purgeDownloadResult()]
        async with cls._qbit_lock:
            if cls.qbittorrent is not None:
                await cls.qbittorrent.torrents.delete("all", False)
        await gather(*operations)
        downloads = []
        results = await gather(cls.aria2.tellActive(), cls.aria2.tellWaiting(0, 1000))
        for res in results:
            downloads.extend(res)
        tasks = []
        tasks.extend(
            cls.aria2.forceRemove(download.get("gid")) for download in downloads
        )
        try:
            await gather(*tasks)
        except:
            pass

    @classmethod
    async def overall_speed(cls):
        s2 = await cls.aria2.getGlobalStat() if cls.aria2 is not None else {}
        download_speed = int(s2.get("downloadSpeed", "0"))
        upload_speed = int(s2.get("uploadSpeed", "0"))
        async with cls._qbit_lock:
            if cls.qbittorrent is not None:
                s1 = await cls.qbittorrent.transfer.info()
                download_speed += s1.dl_info_speed
                upload_speed += s1.up_info_speed
        return download_speed, upload_speed

    @classmethod
    async def pause_all(cls):
        operations = [cls.aria2.forcePauseAll()] if cls.aria2 is not None else []
        async with cls._qbit_lock:
            if cls.qbittorrent is not None:
                await cls.qbittorrent.torrents.stop("all")
        await gather(*operations)

    @classmethod
    async def change_aria2_option(cls, key, value):
        downloads = []
        results = await gather(cls.aria2.tellActive(), cls.aria2.tellWaiting(0, 1000))
        for res in results:
            downloads.extend(res)
            tasks = []
        for download in downloads:
            if download.get("status", "") != "complete":
                tasks.append(cls.aria2.changeOption(download.get("gid"), {key: value}))
        if tasks:
            try:
                await gather(*tasks)
            except Exception as e:
                LOGGER.error(e)
        if key not in ["checksum", "index-out", "out", "pause", "select-file"]:
            await cls.aria2.changeGlobalOption({key: value})
            aria2_options[key] = value


def aria2_name(download_info):
    if "bittorrent" in download_info and download_info["bittorrent"].get("info"):
        return download_info["bittorrent"]["info"]["name"]
    elif download_info.get("files"):
        if download_info["files"][0]["path"].startswith("[METADATA]"):
            return download_info["files"][0]["path"]
        file_path = download_info["files"][0]["path"]
        dir_path = download_info["dir"]
        if file_path.startswith(dir_path):
            return Path(file_path[len(dir_path) + 1 :]).parts[0]
        else:
            return ""
    else:
        return ""


def is_metadata(download_info):
    return any(
        f["path"].startswith("[METADATA]") for f in download_info.get("files", [])
    )
