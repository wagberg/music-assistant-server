#!/usr/bin/env python3
"""
Verify that -http_persistent 0 fixes real Nextory chapters. Requires credentials.

Runs ffmpeg's native HLS demuxer over a real chapter twice -- once with default
options, once with ``-http_persistent 0`` -- and compares both against the chapter
duration reported by the audio package.

Measured on book 86834 (Harry Potter och De Vises Sten):

    chapter 1: default 1826.04s (-17.3%, 453 errors) | fixed 2208.31s (0 errors)
    chapter 2: default  993.50s (-36.9%, 235 errors) | fixed 1574.01s (0 errors)

Usage
-----
    .venv/bin/python scripts/nextory_debug/verify_fix.py 1
    .venv/bin/python scripts/nextory_debug/verify_fix.py 1 2 5
"""
# ruff: noqa: T201, S603, S607

from __future__ import annotations

import asyncio
import pathlib
import subprocess
import sys

import aiohttp

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import (
    FFMPEG_PROTOCOLS,
    auth_headers_arg,
    get_hls_chapters,
    load_creds,
    make_client,
)

WORK = pathlib.Path(__file__).parent / "_work_live"
DEFAULT_BOOK = 86834

CASES = [
    ("default (buggy)", []),
    ("-http_persistent 0 (fix)", ["-http_persistent", "0"]),
]


def run_ffmpeg(
    url: str, headers: str, out: pathlib.Path, extra: list[str]
) -> tuple[float, int, int]:
    """Decode an HLS chapter to PCM. :returns: (seconds, error_count, returncode)."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-headers",
            headers,
            "-allowed_extensions",
            "ALL",
            "-protocol_whitelist",
            FFMPEG_PROTOCOLS,
            *extra,
            "-i",
            url,
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "44100",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    secs = out.stat().st_size / 88200 if out.exists() else 0.0
    errs = proc.stderr.count("Invalid data") + proc.stderr.count("frame header")
    return secs, errs, proc.returncode


async def main() -> None:
    """Run each requested chapter with and without the fix and compare."""
    chapters = [int(a) for a in sys.argv[1:] if a.isdigit()] or [1]
    WORK.mkdir(parents=True, exist_ok=True)

    creds = load_creds()
    async with aiohttp.ClientSession() as session:
        client = await make_client(creds, session)
        _fmt, package = await get_hls_chapters(client, DEFAULT_BOOK)
        headers = auth_headers_arg(client)
        targets = [(n, package.files[n - 1]) for n in chapters]

    for n, chapter in targets:
        expected = chapter.duration / 1000
        print(f"\nchapter {n}: expected {expected:.2f}s")
        for label, extra in CASES:
            out = WORK / f"verify_ch{n:02d}_{'fix' if extra else 'default'}.pcm"
            secs, errs, rc = run_ffmpeg(chapter.uri, headers, out, extra)
            lost = expected - secs
            pct = 100 * lost / expected if expected else 0
            verdict = "OK" if abs(lost) < 1.0 else f"*** LOST {lost:.2f}s ({pct:.1f}%) ***"
            print(f"  {label:26s} -> {secs:8.2f}s  errs={errs:4d} rc={rc}  {verdict}")
    print(f"\nartifacts in {WORK} (gitignored)")


if __name__ == "__main__":
    asyncio.run(main())
