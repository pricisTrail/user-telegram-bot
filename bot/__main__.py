from . import LOGGER, bot_loop
from .core.telegram_manager import TgClient
from .core.config_manager import Config
from os import environ
from deploy.koyeb_profile import low_memory_enabled

KOYEB_MODE = environ.get("MLTB_KOYEB", "").lower() in {"true", "1", "yes", "on"}
if KOYEB_MODE:
    from deploy.koyeb_environment import clear_readiness

    # Also cover direct restarts of the bot process.
    clear_readiness()

Config.load()


async def main():
    from asyncio import gather, wait_for
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
    updates = [update_aria2_options()]
    if not low_memory_enabled():
        updates.extend((update_qb_options(), update_nzb_options()))
    await gather(*updates)
    if low_memory_enabled():
        from .core.engine_manager import start_idle_monitor
        start_idle_monitor()
    from .helper.ext_utils.files_utils import clean_all
    from .modules import (
        initiate_search_tools,
        get_packages_version,
        restart_notification,
    )

    background = [save_settings(), clean_all(), restart_notification()]
    if not low_memory_enabled() or (Config.JD_EMAIL and Config.JD_PASS):
        from .core.jdownloader_booter import jdownloader
        background.append(jdownloader.boot())
    if not low_memory_enabled():
        from .helper.ext_utils.telegraph_helper import telegraph
        background.extend((initiate_search_tools(), get_packages_version(), telegraph.create_account()))
    if not low_memory_enabled() or Config.RCLONE_SERVE_URL:
        from .helper.mirror_leech_utils.rclone_utils.serve import rclone_serve_booter
        background.append(rclone_serve_booter())
    # RSS subscriptions must resume even though command modules are now lazy.
    from . import rss_dict
    if rss_dict:
        from .modules import rss
    await gather(*background)

    from .core.command_menu import install_command_menu

    try:
        await wait_for(
            install_command_menu(TgClient.bot, Config.OWNER_ID, Config.CMD_SUFFIX),
            timeout=30,
        )
        LOGGER.info("Telegram command menu published and verified")
    except Exception as exc:
        # Menu preferences must not prevent transfers or expose private peer IDs.
        LOGGER.warning("Telegram command menu refresh failed (%s)", type(exc).__name__)


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
