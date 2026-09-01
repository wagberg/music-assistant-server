#!/usr/bin/env python3
"""
Live Nextory stream diagnostic. Requires credentials.

Credentials are read from disk by ``_common.load_creds`` and are never printed --
only key names appear in output. See README.md.

Modes
-----
--scan
    Metadata only, no segment downloads. For every chapter, compares the sum of
    ``#EXTINF`` durations in the media playlist against the chapter duration the
    audio package reports. A mismatch would mean the timeline is wrong at the
    source rather than in ffmpeg. (Measured clean: all 17 chapters of book 86834
    match to the millisecond.)

--chapter N
    Deep dive on one chapter. Downloads every segment, decrypts in Python, walks
    ADTS frame headers to derive each segment's TRUE duration, and compares to
    ``#EXTINF``. Flags short segments and mid-chapter sample-rate changes. Then
    runs ffmpeg's native HLS demuxer over the same chapter and diffs the result.

Usage
-----
    .venv/bin/python scripts/nextory_debug/diagnose.py --scan
    .venv/bin/python scripts/nextory_debug/diagnose.py --chapter 1
"""
# ruff: noqa: T201, PLR0915

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import (
    auth_headers_arg,
    decode_duration,
    get_hls_chapters,
    load_creds,
    make_client,
    media_playlist_url,
    parse_adts,
    parse_media_playlist,
)

WORK = pathlib.Path(__file__).parent / "_work_live"
DEFAULT_BOOK = 86834  # harry-potter-och-de-vises-sten (known to trigger the bug)


async def scan(client: object, package: object, session: aiohttp.ClientSession) -> None:
    """Compare EXTINF sums against package chapter durations for every chapter."""
    print(f"\n{'ch':>3} {'EXTINF sum':>11} {'pkg dur':>10} {'delta':>9} {'segs':>5}  verdict")
    print("-" * 62)
    tot_ext = tot_pkg = 0.0
    for i, chapter in enumerate(package.files, 1):  # type: ignore[attr-defined]
        url = media_playlist_url(chapter.uri)
        async with session.get(url, headers=client.auth_headers) as resp:  # type: ignore[attr-defined]
            text = await resp.text()
        segs, durs, _ = parse_media_playlist(text)
        ext = sum(durs)
        pkg = chapter.duration / 1000
        tot_ext += ext
        tot_pkg += pkg
        delta = ext - pkg
        verdict = "OK" if abs(delta) < 0.5 else f"*** {delta:+.1f}s ***"
        print(f"{i:3d} {ext:11.3f} {pkg:10.3f} {delta:+9.3f} {len(segs):5d}  {verdict}")
        (WORK / f"ch{i:02d}.m3u8").write_text(text)
    print("-" * 62)
    print(f"TOTAL EXTINF={tot_ext:.1f}s  PKG={tot_pkg:.1f}s  delta={tot_ext - tot_pkg:+.1f}s")
    print(f"\nplaylists saved to {WORK}")


async def deep_dive(
    client: object, package: object, session: aiohttp.ClientSession, chapter_no: int
) -> None:
    """Per-segment ADTS analysis plus an ffmpeg-native comparison."""
    chapter = package.files[chapter_no - 1]  # type: ignore[attr-defined]
    url = media_playlist_url(chapter.uri)
    async with session.get(url, headers=client.auth_headers) as resp:  # type: ignore[attr-defined]
        text = await resp.text()
    segs, durs, key_url = parse_media_playlist(text)

    key = None
    if key_url:
        async with session.get(key_url, headers=client.auth_headers) as resp:  # type: ignore[attr-defined]
            key = await resp.read()
    print(
        f"\nchapter {chapter_no}: {len(segs)} segments, EXTINF sum {sum(durs):.3f}s, "
        f"pkg duration {chapter.duration / 1000:.3f}s"
    )
    print(f"key fetched: {bool(key)} ({len(key) if key else 0} bytes)")
    n_plain = sum(1 for _, iv in segs if iv is None)
    print(f"unencrypted segments: {n_plain} / {len(segs)} (Nextory pattern: every 6th)\n")

    ref = WORK / f"ch{chapter_no:02d}_ref.aac"
    print(
        f"{'seg':>4} {'enc':>8} {'dec':>8} {'frames':>7} {'adts_dur':>9} "
        f"{'extinf':>8} {'delta':>8} {'junk':>5} rates"
    )
    print("-" * 88)
    all_rates: set[int] = set()
    adts_total = 0.0
    bad = 0
    with ref.open("wb") as out:
        for i, ((seg_url, iv), extinf) in enumerate(zip(segs, durs, strict=True)):
            async with session.get(seg_url) as resp:
                if resp.status != 200:
                    print(
                        f"{i:4d}  HTTP {resp.status} -- aborting (presigned URL may have expired)"
                    )
                    break
                raw = await resp.read()
            enc_n = len(raw)
            if iv is not None and key:
                dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
                plain = dec.update(raw) + dec.finalize()
                try:
                    unpad = PKCS7(128).unpadder()
                    raw = unpad.update(plain) + unpad.finalize()
                except ValueError:
                    print(f"{i:4d}  PKCS7 UNPAD FAILED")
                    raw = plain
            out.write(raw)
            frames, adts_dur, rates, junk = parse_adts(raw)
            all_rates |= rates
            adts_total += adts_dur
            delta = adts_dur - extinf
            flag = ""
            if abs(delta) >= 0.05:
                flag = "  <<< SHORT"
                bad += 1
            print(
                f"{i:4d} {enc_n:8d} {len(raw):8d} {frames:7d} {adts_dur:9.3f} "
                f"{extinf:8.3f} {delta:+8.3f} {junk:5d} {sorted(rates)}{flag}"
            )
    print("-" * 88)
    print(
        f"ADTS total {adts_total:.3f}s vs EXTINF {sum(durs):.3f}s "
        f"(delta {adts_total - sum(durs):+.3f}s), short segments: {bad}"
    )
    print(
        f"sample rates seen: {sorted(all_rates)}"
        f"{'   <<< MIXED RATES' if len(all_rates) > 1 else ''}"
    )

    headers = auth_headers_arg(client)
    ff_s, _, _ = decode_duration(
        chapter.uri,
        WORK / f"ch{chapter_no:02d}_ffmpeg.pcm",
        hls=True,
        extra=["-headers", headers],
    )
    ref_s, _, _ = decode_duration(str(ref), WORK / f"ch{chapter_no:02d}_ref.pcm", hls=False)
    print(f"\npython-decrypt decode : {ref_s:8.2f}s")
    print(f"ffmpeg native HLS     : {ff_s:8.2f}s   (with -http_persistent 0)")
    print(
        f"difference            : {ff_s - ref_s:+8.2f}s"
        f"{'   <<< ffmpeg IS LOSING AUDIO' if ref_s - ff_s > 1 else ''}"
    )
    print(f"\nartifacts in {WORK} (gitignored)")


async def main() -> None:
    """Parse arguments and dispatch to scan or deep-dive mode."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan", action="store_true", help="metadata-only sweep (cheap)")
    ap.add_argument("--chapter", type=int, help="deep dive on one chapter")
    ap.add_argument("--book", type=int, default=DEFAULT_BOOK)
    args = ap.parse_args()
    if not args.scan and args.chapter is None:
        args.scan = True

    WORK.mkdir(parents=True, exist_ok=True)
    creds = load_creds()
    async with aiohttp.ClientSession() as session:
        client = await make_client(creds, session)
        _fmt, package = await get_hls_chapters(client, args.book)
        if args.scan:
            await scan(client, package, session)
        else:
            await deep_dive(client, package, session, args.chapter)


if __name__ == "__main__":
    asyncio.run(main())
