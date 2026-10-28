"""Publish commands for a Telegram identity reused by this application."""

from re import fullmatch


MENU_ENTRIES = (
    ("start", "Start Trail Mirror Leech"),
    ("help", "Show commands and usage"),
    ("leech", "Download links or files to Telegram"),
    ("ffl", "Leech DASH, HLS or media using FFmpeg"),
    ("qbleech", "Download torrents to Telegram"),
    ("ytdlleech", "Download videos to Telegram"),
    ("gallerydlleech", "Download galleries to Telegram"),
    ("mirror", "Upload to cloud after Drive or rclone setup"),
    ("qbmirror", "Mirror torrents after cloud setup"),
    ("status", "Show active transfers"),
    ("cancel", "Cancel a transfer by reply or task ID"),
    ("sel", "Select files from a torrent"),
    ("usetting", "Change your upload preferences"),
    ("stats", "Show server resource usage"),
)


def command_entries(suffix=""):
    entries = [(command + suffix, description) for command, description in MENU_ENTRIES]
    if any(not fullmatch(r"[a-z0-9_]{1,32}", command) for command, _ in entries):
        raise ValueError("CMD_SUFFIX produces invalid Telegram menu commands")
    return entries


async def install_command_menu(client, owner_id, suffix=""):
    from pyrogram.types import (
        BotCommand,
        BotCommandScopeAllPrivateChats,
        BotCommandScopeChat,
        BotCommandScopeDefault,
    )

    entries = command_entries(suffix)
    commands = [BotCommand(command, description) for command, description in entries]
    scopes = (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeChat(owner_id),
    )
    owner_resolved = False
    for language in ("", "en"):
        for scope in scopes:
            if isinstance(scope, BotCommandScopeChat) and not owner_resolved:
                # New containers have a fresh peer cache, even for existing bots.
                await client.get_users(owner_id)
                owner_resolved = True
            published = await client.set_bot_commands(
                commands, scope=scope, language_code=language
            )
            if published is False:
                raise RuntimeError("Telegram rejected the command menu")
            actual = await client.get_bot_commands(scope=scope, language_code=language)
            if [(item.command, item.description) for item in actual] != entries:
                raise RuntimeError("Telegram command menu readback did not match")
    return 6
