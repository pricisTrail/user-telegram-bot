#!/usr/bin/env bash

# A slow tracker-list host must not block download-engine/HTTP startup.
# An empty list is valid; magnets and torrents can supply their own trackers.
tracker_list=$(
    { curl --fail --silent --location --connect-timeout 5 --max-time 15 \
        https://ngosang.github.io/trackerslist/trackers_all_http.txt || true; } |
        awk '$0' | tr '\n\n' ','
)

# Leave the original profile intact unless the constrained-host option is on.
low_memory_mode="${MLTB_LOW_MEMORY:-false}"
low_memory_mode="${low_memory_mode#"${low_memory_mode%%[![:space:]]*}"}"
low_memory_mode="${low_memory_mode%"${low_memory_mode##*[![:space:]]}"}"
case "${low_memory_mode,,}" in
    true|1|yes|on)
        aria_peers=32
        aria_rpc_size=16M
        aria_connections=2
        aria_downloads=1
        aria_split=2
        aria_cache=8M
        aria_optimize=false
        ;;
    *)
        aria_peers=0
        aria_rpc_size=1024M
        aria_connections=10
        aria_downloads=1000
        aria_split=10
        aria_cache=40M
        aria_optimize=true
        ;;
esac

aria2c --allow-overwrite=true --auto-file-renaming=true --bt-enable-lpd=true --bt-detach-seed-only=true \
       --bt-remove-unselected-file=true --bt-tracker="[$tracker_list]" --bt-max-peers="$aria_peers" --enable-rpc=true \
       --rpc-max-request-size="$aria_rpc_size" --max-connection-per-server="$aria_connections" --max-concurrent-downloads="$aria_downloads" --split="$aria_split" \
       --seed-ratio=0 --check-integrity=true --continue=true --daemon=true --disk-cache="$aria_cache" --force-save=true \
       --min-split-size=10M --follow-torrent=mem --check-certificate=false --optimize-concurrent-downloads="$aria_optimize" \
       --http-accept-gzip=true --max-file-not-found=0 --max-tries=20  --peer-id-prefix=-qB4520- --reuse-uri=true \
       --content-disposition-default-utf8=true --user-agent=Wget/1.12 --peer-agent=qBittorrent/4.5.2 --quiet=true \
       --summary-interval=0 --max-upload-limit=1K
qbittorrent-nox -d --profile="$(pwd)"
sabnzbdplus -f sabnzbd/SABnzbd.ini -s :::8070 -b 0 -d -c -l 0 --console
