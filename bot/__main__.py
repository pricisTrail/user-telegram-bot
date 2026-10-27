from . import LOGGER, bot_loop
from .core.telegram_manager import TgClient
from .core.config_manager import Config
from os import environ

KOYEB_MODE = environ.get("MLTB_KOYEB", "").lower() in {"true", "1", "yes", "on"}
if KOYEB_MODE:
    from deploy.koyeb_environment import clear_readiness

    # Also cover direct restarts of the bot process.
    clear_readiness()

Config.load()


async def main():
    from asyncio import gather
    from .core.startup import (
        load_settings,
        load_configurations,
        save_settings,
        update_aria2_options,
        update_nzb_options,
        update_qb_options,
        update_variables,
    )

    await load_settings()

    # A saved Mongo configuration must not restore an obsolete Koyeb URL/port.
    if KOYEB_MODE:
        from deploy.koyeb_environment import apply_platform_config

        apply_platform_config(Config, environ)

    await gather(TgClient.start_bot(), TgClient.start_user())
    await gather(load_configurations(), update_variables())

    from .core.torrent_manager import TorrentManager

    await TorrentManager.initiate()
    await gather(
        update_qb_options(),
        update_aria2_options(),
        update_nzb_options(),
    )
    from .helper.ext_utils.files_utils import clean_all
    from .core.jdownloader_booter import jdownloader
    from .helper.ext_utils.telegraph_helper import telegraph
    from .helper.mirror_leech_utils.rclone_utils.serve import rclone_serve_booter
    from .modules import (
        initiate_search_tools,
        get_packages_version,
        restart_notification,
    )

    await gather(
        save_settings(),
        jdownloader.boot(),
        clean_all(),
        initiate_search_tools(),
        get_packages_version(),
        restart_notification(),
        telegraph.create_account(),
        rclone_serve_booter(),
    )


bot_loop.run_until_complete(main())

from .helper.ext_utils.bot_utils import create_help_buttons
from .helper.listeners.aria2_listener import add_aria2_callbacks
from .core.handlers import add_handlers

add_aria2_callbacks()
create_help_buttons()
add_handlers()

LOGGER.info(f"Bot Started! @{TgClient.NAME}")
if KOYEB_MODE:
    from pathlib import Path
    from deploy.koyeb_environment import READINESS_FILENAME

    Path(READINESS_FILENAME).write_text("ready", encoding="utf-8")
bot_loop.run_forever()
