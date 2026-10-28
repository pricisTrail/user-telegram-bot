"""Parse the small /ffl interface without exposing media URLs in errors."""

from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import PurePosixPath
from shlex import split
from urllib.parse import urlsplit


@dataclass(frozen=True)
class FFmpegRequest:
    url: str = field(repr=False)
    name: str = ""
    max_height: int = 720


def validate_media_url(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("Use a valid HTTP or HTTPS media URL.") from None
    if (
        len(url) > 8192
        or any(char.isspace() or ord(char) < 32 for char in url)
        or parsed.scheme.lower() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65535)
        or host.lower().rstrip(".").endswith(("localhost", ".local"))
    ):
        raise ValueError("Use a public HTTP or HTTPS media URL without embedded credentials.")
    try:
        address = ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("Local and private IP addresses are not supported.")
    return url


def safe_output_name(name):
    name = name.strip()
    if (
        not name
        or len(name) > 180
        or name.startswith((".", "-"))
        or any(char in '/\\:*?"<>|' or ord(char) < 32 for char in name)
    ):
        raise ValueError("Use a plain filename without folders or special path characters.")
    suffix = PurePosixPath(name).suffix.lower()
    if not suffix:
        name += ".mp4"
    elif suffix not in {".mp4", ".mkv"}:
        raise ValueError("The output filename must end in .mp4 or .mkv.")
    return name


def parse_ffmpeg_request(text, reply_text=None):
    try:
        tokens = split((text or "").splitlines()[0])
    except (ValueError, IndexError):
        raise ValueError("Check the command and quote filenames containing spaces.") from None
    if not tokens:
        raise ValueError("Provide a media URL or reply to a message containing one.")
    tokens = tokens[1:]
    url = ""
    name = ""
    max_height = 720
    seen = set()
    while tokens:
        token = tokens.pop(0)
        if token in {"-q", "-n"}:
            if token in seen or not tokens:
                raise ValueError("Each option needs one value and can be used only once.")
            seen.add(token)
            value = tokens.pop(0)
            if token == "-n":
                name = safe_output_name(value)
            elif value.lower() == "best":
                max_height = 0
            else:
                try:
                    max_height = int(value)
                except ValueError:
                    raise ValueError("Quality must be a height such as 720, or best.") from None
                if not 144 <= max_height <= 4320:
                    raise ValueError("Quality must be between 144 and 4320, or best.")
        elif token.startswith("-"):
            raise ValueError("Supported options are -q and -n only.")
        elif url:
            raise ValueError("Provide one media URL; quote filenames containing spaces.")
        else:
            url = token
    if not url and reply_text:
        parts = reply_text.strip().split()
        url = parts[0] if parts else ""
    if not url:
        raise ValueError("Provide a media URL or reply to a message containing one.")
    return FFmpegRequest(validate_media_url(url), name, max_height)
