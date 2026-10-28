# Mirror/Leech Telegram Bot on Koyeb

This fork retains the original Python bot and its download engines. It adds a
Koyeb-specific Dockerfile, environment bootstrap, and opt-in low-memory profile.
There is no Bun wrapper or language conversion. The original `Dockerfile` and
`start.sh` remain available for existing deployments.

Upstream: [anasty17/mirror-leech-telegram-bot](https://github.com/anasty17/mirror-leech-telegram-bot).
The project retains its GPL-3.0 license.

## Deploy to Koyeb

[![Deploy to Koyeb](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?type=git&builder=dockerfile&repository=github.com%2FpricisTrail%2Fuser-telegram-bot&branch=koyeb-ready&name=mirror-leech&service_type=web&dockerfile=Dockerfile.koyeb&instance_type=free&regions=fra&ports=8080%3Bhttp%3B%2F&env%5BMLTB_LOW_MEMORY%5D=true&env%5BPORT%5D=8080&hc_protocol%5B8080%5D=http&hc_path%5B8080%5D=%2Fhealthz&hc_grace_period%5B8080%5D=120&hc_interval%5B8080%5D=30&hc_timeout%5B8080%5D=5&hc_restart_limit%5B8080%5D=3&hc_method%5B8080%5D=get)

The button uses the `koyeb-ready` branch. If you deploy a merged copy from another
branch, select that branch in Koyeb's Source settings.

1. Select `pricisTrail/user-telegram-bot` and branch `koyeb-ready`.
2. Choose a **Web Service**, the **Dockerfile** builder, and Dockerfile location
   **`Dockerfile.koyeb`**. Keep the image's default command and entrypoint.
   Privileged mode is not required.
3. Expose **HTTP port 8080**, routed at `/`. Use an HTTP health check on `/healthz` and
   allow a startup grace period of **120 seconds** while the engines initialize.
4. Add the four required environment variables below. The
   [bulk-edit example](deploy/koyeb.env.example) lists the optional settings too.
   Enter credentials in Koyeb's settings or secrets, never in GitHub files.
5. Deploy and inspect the runtime logs for `Bot Started!`. Send `/start` to your
   dedicated mirror/leech bot, then test a small `/leech` job. Configure an upload
   destination before testing `/mirror`.

| Required variable | Value |
|---|---|
| `BOT_TOKEN` | Token for a separate mirror/leech bot from BotFather |
| `OWNER_ID` | Your numeric Telegram user ID |
| `TELEGRAM_API` | Telegram application API ID |
| `TELEGRAM_HASH` | Matching application API hash |

Do not reuse a token from a running file-to-link bot: two different applications
should not operate the same Telegram bot identity concurrently.

### Public address and port

The Koyeb bootstrap fills `BASE_URL` from `https://` plus Koyeb's
`KOYEB_PUBLIC_DOMAIN`. You can instead set `BASE_URL` explicitly to your public
HTTPS address. `BASE_URL_PORT` takes priority over `PORT`; otherwise `PORT` is
used, falling back to `8080`. The exposed Koyeb port must match this value.

The original bot starts its web server only when `BASE_URL` is set, and listens on
`0.0.0.0:BASE_URL_PORT`. Setting `PORT` alone without this bootstrap does not
configure the original bot's web server.

`config.py` is excluded from this Docker build. The upstream loader uses that
file instead of environment variables when it exists, so the Koyeb entrypoint
rejects a mounted `config.py` with an actionable error. Keep configuration in
Koyeb variables and private files in runtime storage.

After saved MongoDB settings are restored, the current platform URL and port are
reapplied. The `/healthz` endpoint returns `503` while initialization is pending
and `200` only after the bot's handlers are registered. The bootstrap removes a
stale readiness marker on every start. The existing `/` page is informational.

### Low-memory profile

`Dockerfile.koyeb` sets `MLTB_LOW_MEMORY=true`. This limits the task queue and
reduces aria2 and qBittorrent concurrency, cache sizes, threads, and peer counts.
It also lowers SABnzbd's cache. The profile is applied after MongoDB settings are
restored, so saved engine preferences do not undo these startup limits.
Telegram clients use two update workers, one simultaneous transfer, and smaller
message/topic caches in this mode.

This is a conservative starting profile, not a guaranteed RAM ceiling. The bot
still starts aria2, qBittorrent, and SABnzbd; large transfers, extraction, media
processing, or many files can exceed a small instance's memory or disk. Keep
`JD_EMAIL` and `JD_PASS` empty to avoid starting JDownloader's Java process.
On a larger instance, set `MLTB_LOW_MEMORY=false` and adjust the queue settings
explicitly if you want more concurrency.

### MongoDB, uploads, and storage

- MongoDB is optional. Set `DATABASE_URL` to use it, with a separate
  `DATABASE_NAME` such as `mltb`. Do not use a file-to-link service's `ThunderF2L`
  database. Saved application settings can override environment settings for the
  same bot identity; the low-memory startup caps are enforced separately.
- `/leech` uploads to Telegram. `/mirror` needs an upload integration, such as an
  rclone configuration and `RCLONE_PATH`, or Google Drive credentials with
  `GDRIVE_ID` and `DEFAULT_UPLOAD=gd`. This preparation does not provision cloud
  accounts or import credentials.
- Sessions, downloaded files, cookies, tokens, and private configurations are
  excluded from the Koyeb build context. Restore private files through the bot's
  existing private-file/Mongo workflow or appropriate runtime storage.
- Koyeb's local disk is ephemeral. Redeployments can remove sessions and
  in-progress downloads. Downloaded files, split parts, extracted files, the
  container image, and dependencies all compete for available disk space.
- Keep `RCLONE_SERVE_PORT` different from the main web port if enabling rclone's
  HTTP server; its upstream default is also `8080`.

### FFmpeg leech command

Use `/ffl` for finite HTTP(S) DASH manifests (`.mpd`), HLS playlists (`.m3u8`),
or direct media files. The bot selects an existing video track and audio, copies
them into one file without transcoding, and uses its normal Telegram upload
pipeline. The download queue, `/status`, and `/cancel` also apply.

```text
/ffl https://media.example.org/manifest.mpd
/ffl https://media.example.org/manifest.mpd -q 1080 -n "My video.mp4"
/ffl https://media.example.org/playlist.m3u8 -q best -n "My video.mkv"
```

You can also reply to a message containing a media URL with `/ffl`. The default
quality ceiling is **720p**, chosen for the small Koyeb instance. `-q best`
selects the highest available representation; it can produce a much larger file.
`-n` accepts a plain `.mp4` or `.mkv` filename. MP4 is the default; try MKV when
the source's codecs cannot be copied into MP4.

In low-memory mode, `/ffl` reuses the selected video's dimensions and duration
when uploading its unchanged output file, and skips automatic thumbnail
generation. A saved user thumbnail is still used. Other uploads and processed
or split files retain their existing media inspection.

The command does not accept arbitrary FFmpeg flags, local paths, or embedded
login credentials. URLs and FFmpeg diagnostics are kept out of its logs and
error messages. FFprobe inspection is bounded, a job stops after one hour, and
disk checks retain a **256 MiB reserve**. Failed downloads and detected size or
duration shortfalls are removed instead of being uploaded as successful results. Live
streams and media that require decryption or additional access configuration
are not supported by this command. No new environment variables are required.

### Free-instance limits

Koyeb currently documents **512 MB RAM, 0.1 vCPU, and 2 GB SSD**, one Free instance
per organization, Web Services only, no persistent volumes, and scale-to-zero
after one hour without incoming traffic. An outbound Telegram connection does
not provide a reliable wake-up path; open the public service URL if it sleeps.
Do not replace another running bot merely to free the allowance without first
deciding to retire that service. This repository preparation does not modify
any live Koyeb services.

The upstream base image includes multiple native programs. Its unpacked image
size and this fork's runtime resource use must be checked on the actual instance;
low-memory settings do not prove that the full image fits the Free instance.
Use adequate resources for large or continuous workloads.

## Updates and verification

The Koyeb entrypoint starts the code built into the image, without running the
upstream Git updater before startup. Leave `UPSTREAM_REPO` empty and update by
deploying a reviewed Git commit. A source or requirements change needs a rebuild;
an environment-only change can use Koyeb's **Deploy without rebuild** option.

The bot publishes its mirror/leech command menu at startup, including the
default, private-chat, and owner-chat menus in the default language and English.
This replaces command lists left by an earlier bot application using the same
Telegram identity. Cloud mirror entries explicitly require Drive or rclone
setup. Menu publication is checked against Telegram, respects `CMD_SUFFIX`, and
cannot prevent the bot from starting if Telegram rejects a menu update.

Focused deployment checks run without Telegram credentials or Linux services:

```sh
python3 -m unittest discover -s tests/koyeb -v
```

These checks do not establish a successful container deployment. Verify image
build, startup, RAM/disk consumption, `/start`, a small download/upload, and the
rendered file-selection page on Koyeb. UI verification remains manual.

References: [Koyeb Git deployments](https://www.koyeb.com/docs/build-and-deploy/deploy-with-git),
[environment variables](https://www.koyeb.com/docs/build-and-deploy/environment-variables),
[instance limits](https://www.koyeb.com/docs/reference/instances),
[upstream documentation](https://github.com/anasty17/mirror-leech-telegram-bot#readme).
