"""Shared helpers for the Nextory streaming debug scripts.

Kept dependency-light and self-contained so each script can be run directly with
the project venv:  .venv/bin/python scripts/nextory_debug/<script>.py
"""
# ruff: noqa: T201, S603, PLC0415

from __future__ import annotations

import json
import pathlib
import re
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiohttp

# ADTS sampling-frequency index table (ISO/IEC 13818-7)
ADTS_RATES = [
    96000,
    88200,
    64000,
    48000,
    44100,
    32000,
    24000,
    22050,
    16000,
    12000,
    11025,
    8000,
    7350,
    0,
    0,
    0,
]

#: Credential file locations, searched in order. Values are NEVER printed.
CRED_PATHS = [
    pathlib.Path.home() / ".config" / "nextory" / "profile.yaml",
    pathlib.Path.home() / ".config" / "nextory" / "profile.json",
    pathlib.Path.home() / ".config" / "nextory-ma-debug" / "creds.json",
]

#: Protocols ffmpeg needs for encrypted HLS. Mirrors MA's own whitelist.
FFMPEG_PROTOCOLS = "file,hls,crypto,data,http,https,tls,tcp"

#: The fix. Without this ffmpeg silently discards 17-37% of a Nextory chapter.
FFMPEG_HLS_ARGS = [
    "-allowed_extensions",
    "ALL",
    "-protocol_whitelist",
    FFMPEG_PROTOCOLS,
    "-http_persistent",
    "0",
]


def parse_adts(data: bytes) -> tuple[int, float, set[int], int]:
    """Walk raw ADTS frames and derive an exact duration.

    ffprobe's ``format=duration`` is a bitrate estimate on raw ADTS and can be
    several seconds out, so frame walking is the only reliable measurement.

    :param data: Raw ADTS (packed AAC) bytes.
    :returns: ``(frame_count, duration_seconds, sample_rates_seen, trailing_junk_bytes)``.
    """
    off = 0
    frames = 0
    dur = 0.0
    rates: set[int] = set()
    n = len(data)
    while off + 7 <= n:
        if data[off] != 0xFF or (data[off + 1] & 0xF0) != 0xF0:
            break  # lost sync
        rate = ADTS_RATES[(data[off + 2] >> 2) & 0x0F]
        flen = ((data[off + 3] & 0x03) << 11) | (data[off + 4] << 3) | (data[off + 5] >> 5)
        if flen < 7 or off + flen > n:
            break
        rates.add(rate)
        if rate:
            dur += 1024 / rate
        frames += 1
        off += flen
    return frames, dur, rates, n - off


def parse_media_playlist(
    text: str,
) -> tuple[list[tuple[str, bytes | None]], list[float], str | None]:
    """Parse an HLS media playlist.

    Tracks encryption state per segment: Nextory emits ``METHOD=NONE`` for every
    6th segment and ``METHOD=AES-128`` (with a per-segment IV) for the rest.

    :param text: Playlist body.
    :returns: ``(segments[(url, iv_or_None)], extinf_durations, key_url_or_None)``.
    """
    segs: list[tuple[str, bytes | None]] = []
    durs: list[float] = []
    iv: bytes | None = None
    encrypted = False
    key_url: str | None = None
    pending = 0.0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-KEY"):
            if "METHOD=NONE" in line:
                encrypted = False
            elif "METHOD=AES-128" in line:
                encrypted = True
                if m := re.search(r'URI="([^"]+)"', line):
                    key_url = m.group(1)
                if m := re.search(r"IV=0x([0-9a-fA-F]+)", line):
                    iv = bytes.fromhex(m.group(1))
        elif line.startswith("#EXTINF:"):
            pending = float(line.split(":")[1].split(",")[0])
        elif line.startswith("http"):
            segs.append((line, iv if encrypted else None))
            durs.append(pending)
    return segs, durs, key_url


def _parse_flat(text: str) -> dict[str, str]:
    """Parse flat ``key: value`` (YAML subset) or JSON. Avoids a yaml dependency."""
    text = text.strip()
    if text.startswith("{"):
        return {k: str(v) for k, v in json.loads(text).items()}
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def load_creds(*, quiet: bool = False) -> dict[str, str]:
    """Load Nextory credentials from disk, merging the known locations.

    Only key *names* are ever printed, never values.

    :raises SystemExit: If no credential file is found.
    """
    merged: dict[str, str] = {}
    found: list[str] = []
    for path in CRED_PATHS:
        if not path.exists():
            continue
        found.append(str(path))
        for k, v in _parse_flat(path.read_text()).items():
            merged.setdefault(k, v)
    if not merged:
        locations = "\n  ".join(str(p) for p in CRED_PATHS)
        raise SystemExit(
            f"No Nextory credentials found. Looked in:\n  {locations}\n\n"
            "Provide either login_token + profile_token + login_key (preferred, "
            "reuses an existing session) or username + password."
        )
    if not quiet:
        print(f"creds loaded from: {', '.join(found)}")
        print(f"  keys: {sorted(merged)}")
    return merged


async def make_client(creds: dict[str, str], session: aiohttp.ClientSession) -> Any:
    """Build an authenticated NextoryClient, refreshing a stale profile token.

    Prefers stored tokens; falls back to username/password login, which consumes
    a profile session slot on the account.
    """
    from nextory import NextoryClient

    if creds.get("profile_token"):
        client = NextoryClient(
            login_token=creds.get("login_token"),
            login_key=creds.get("login_key"),
            profile_token=creds["profile_token"],
            session=session,
        )
    else:
        client = NextoryClient(session=session)
        await client.login(creds["username"], creds["password"])
        profiles = await client.get_profiles()
        await client.select_profile(profiles.profiles[0].login_key)
        print(f"logged in, selected profile: {profiles.profiles[0].name}")

    try:
        account = await client.get_account()
    except Exception as err:
        print(f"get_account failed ({type(err).__name__}); refreshing profile token")
        if not creds.get("login_key"):
            raise
        await client._refresh_profile_token()
        account = await client.get_account()
    client.country = account.country
    return client


def auth_headers_arg(client: Any) -> str:
    """Render the client's auth headers as an ffmpeg ``-headers`` value.

    WARNING: contains live login/profile tokens. Never commit ffmpeg logs that
    include this value.
    """
    return "".join(f"{k}: {v}\r\n" for k, v in client.auth_headers.items())


async def get_hls_chapters(client: Any, book_id: int) -> tuple[Any, Any]:
    """Resolve a book to its active HLS format and audio package (chapter list)."""
    from nextory.models import FormatState, FormatType

    product = await client.get_product_details(book_id)
    fmt = next(
        (f for f in product.formats if f.type == FormatType.HLS and f.state == FormatState.ACTIVE),
        None,
    )
    if fmt is None:
        raise SystemExit(f"book {book_id} has no ACTIVE HLS format")
    package = await client.get_audio_package(fmt.identifier)
    print(f"book   : {product.title}")
    print(f"format : {fmt.identifier} (declared duration {fmt.duration}s)")
    print(f"package: {len(package.files)} chapters, {package.duration / 1000:.1f}s")
    return fmt, package


def media_playlist_url(master_url: str) -> str:
    """Derive the media playlist URL from a chapter's master playlist URL."""
    return master_url.replace("/master_playlist", "/media_playlist.m3u8")


def decode_duration(
    path_or_url: str, out_pcm: pathlib.Path, *, hls: bool, extra: list[str] | None = None
) -> tuple[float, str, int]:
    """Decode an input to mono 44.1k s16le and return its duration in seconds.

    ``-allowed_extensions`` is an HLS-demuxer option and errors on plain ADTS
    input, so it is only passed when ``hls`` is set.

    :returns: ``(duration_seconds, stderr, returncode)``.
    """
    args = ["ffmpeg", "-y", "-v", "error"]
    if hls:
        args += FFMPEG_HLS_ARGS
    args += [
        *(extra or []),
        "-i",
        str(path_or_url),
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        "44100",
        str(out_pcm),
    ]
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    size = out_pcm.stat().st_size if out_pcm.exists() else 0
    return size / (44100 * 2), proc.stderr, proc.returncode
