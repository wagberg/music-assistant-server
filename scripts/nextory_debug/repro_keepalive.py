#!/usr/bin/env python3
"""Reproduce the ffmpeg encrypted-HLS audio-loss bug WITHOUT Nextory credentials.

Root cause
----------
ffmpeg reuses a keep-alive HTTP connection across ``METHOD=NONE`` -> ``AES-128``
transitions and corrupts the first one or two ENCRYPTED segments after each one.

HTTP keep-alive is the necessary condition: with ``file://`` or an HTTP/1.0 server
that closes each connection, nothing is lost. The number of NONE->AES transitions
sets the magnitude -- one transition costs ~1 segment (~1.8%), while Nextory's
every-6th-segment pattern makes ~62 transitions per chapter and costs 17-37%.

That combination is why this evaded diagnosis: local-file tests and short
single-transition tests both look nearly healthy.

Usage
-----
    .venv/bin/python scripts/nextory_debug/repro_keepalive.py
"""
# ruff: noqa: T201, S603, S607

from __future__ import annotations

import argparse
import functools
import http.server
import os
import pathlib
import shutil
import socketserver
import subprocess
import sys
import threading

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import FFMPEG_PROTOCOLS, parse_adts

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
WORK = pathlib.Path(__file__).parent / "_work_repro"
PORT = 8901
SIGNED = "?nx-expires=1893456000&nx-signature=6f1b2c3d4e5a6b7c8d9e0f1a2b3c4d5e"


class KeepAliveHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP/1.1 => keep-alive enabled, like a real CDN."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        """Silence per-request logging."""


class NoKeepAliveHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP/1.0 => connection closed after each response."""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args: object) -> None:
        """Silence per-request logging."""


class Server(socketserver.ThreadingTCPServer):
    """Threaded server with address reuse so cases can rebind the same port."""

    allow_reuse_address = True
    daemon_threads = True


def sh(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a command and capture its output."""
    return subprocess.run(args, capture_output=True, text=True, check=False)


def make_segments(work: pathlib.Path, n_seg: int, dur: int) -> list[pathlib.Path]:
    """Generate a chirp and slice it into ~6s frame-aligned ADTS segments."""
    src = work / "source.aac"
    sh(
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc=0.5*sin(2*PI*(200+t*20)*t):d={dur}:s=44100",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-f",
        "adts",
        str(src),
    )
    sh(
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        "6",
        "-segment_format",
        "adts",
        str(work / "s_%04d.aac"),
    )
    return sorted(work.glob("s_[0-9][0-9][0-9][0-9].aac"))[:n_seg]


def build_playlist(
    work: pathlib.Path, segs: list[pathlib.Path], plain_every: int, *, over_http: bool
) -> tuple[pathlib.Path, pathlib.Path]:
    """Write a Nextory-shaped playlist plus a plaintext reference.

    :param plain_every: 0 => only segment 0 unencrypted; N => every Nth unencrypted.
    :param over_http: reference segments by http:// URL instead of file://.
    """
    (work / "key.bin").write_bytes(KEY)
    base = f"http://127.0.0.1:{PORT}" if over_http else f"file://{work}"
    key_uri = f"{base}/key.bin"
    suffix = SIGNED if over_http else ""

    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:7", "#EXT-X-MEDIA-SEQUENCE:0"]
    ref = work / "reference.aac"
    with ref.open("wb") as out:
        for i, seg in enumerate(segs):
            raw = seg.read_bytes()
            out.write(raw)
            _, seg_dur, _, _ = parse_adts(raw)
            plain = (i == 0) if plain_every == 0 else (i % plain_every == 0)
            if plain:
                lines.append("#EXT-X-KEY:METHOD=NONE")
                name = f"uncrypted_{i:04d}.aac"
                (work / name).write_bytes(raw)
            else:
                iv = os.urandom(16)
                padder = PKCS7(128).padder()
                enc = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
                name = f"crypted_{i:04d}.aac"
                (work / name).write_bytes(
                    enc.update(padder.update(raw) + padder.finalize()) + enc.finalize()
                )
                lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{key_uri}",IV=0x{iv.hex()}')
            lines.append(f"#EXTINF:{seg_dur:.3f},")
            lines.append(f"{base}/{name}{suffix}")
    lines.append("#EXT-X-ENDLIST")
    playlist = work / "playlist.m3u8"
    playlist.write_text("\n".join(lines) + "\n")
    return playlist, ref


def measure(
    path: pathlib.Path, out: pathlib.Path, *, hls: bool, extra: list[str] | None = None
) -> tuple[float, int]:
    """Decode to PCM. :returns: (duration_seconds, decode_error_count)."""
    args = ["ffmpeg", "-y", "-v", "error"]
    if hls:
        args += ["-allowed_extensions", "ALL", "-protocol_whitelist", FFMPEG_PROTOCOLS]
    args += [*(extra or []), "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "44100", str(out)]
    proc = sh(*args)
    dur = out.stat().st_size / 88200 if out.exists() else 0.0
    errs = proc.stderr.count("Invalid data") + proc.stderr.count("frame header")
    return dur, errs


def case(
    label: str,
    *,
    plain_every: int,
    over_http: bool,
    keep_alive: bool,
    extra: list[str] | None = None,
    n_seg: int = 60,
    dur: int = 360,
) -> None:
    """Build one matrix row and print reference vs HLS durations."""
    work = WORK / label.replace(" ", "_").replace("/", "_")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    segs = make_segments(work, n_seg, dur)
    if not segs:
        print(f"  {label:44s} SEGMENT GENERATION FAILED")
        return
    playlist, ref = build_playlist(work, segs, plain_every, over_http=over_http)

    srv = None
    if over_http:
        # Bind the handler to `work` explicitly; SimpleHTTPRequestHandler resolves
        # its directory at request time, so chdir() would race with the caller.
        handler = functools.partial(
            KeepAliveHandler if keep_alive else NoKeepAliveHandler,
            directory=str(work),
        )
        srv = Server(("127.0.0.1", PORT), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ref_s, _ = measure(ref, work / "ref.pcm", hls=False)
        hls_s, errs = measure(playlist, work / "hls.pcm", hls=True, extra=extra)
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()

    lost = ref_s - hls_s
    pct = (100 * lost / ref_s) if ref_s else 0
    verdict = "clean" if abs(lost) < 0.15 else f"*** LOST {lost:.2f}s ({pct:.1f}%) ***"
    print(f"  {label:44s} ref={ref_s:7.2f}s hls={hls_s:7.2f}s errs={errs:4d}  {verdict}")


def main() -> None:
    """Run the full isolation matrix."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", action="store_true", help="keep generated work dirs")
    args = ap.parse_args()

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    print(__doc__.split("Usage")[0].strip())
    print(
        "\nIsolation matrix (ffmpeg "
        + subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, check=False
        ).stdout.split()[2]
        + "):\n"
    )

    case("file://          single plain segment", plain_every=0, over_http=False, keep_alive=False)
    case("file://          every 6th plain", plain_every=6, over_http=False, keep_alive=False)
    case("http/1.0 no-KA   single plain segment", plain_every=0, over_http=True, keep_alive=False)
    case("http/1.0 no-KA   every 6th plain", plain_every=6, over_http=True, keep_alive=False)
    case("http/1.1 KEEPALIVE  single plain segment", plain_every=0, over_http=True, keep_alive=True)
    case(
        "http/1.1 KEEPALIVE  every 6th plain  <-- BUG",
        plain_every=6,
        over_http=True,
        keep_alive=True,
    )
    case(
        "http/1.1 KEEPALIVE  every 6th + FIX",
        plain_every=6,
        over_http=True,
        keep_alive=True,
        extra=["-http_persistent", "0"],
    )

    print("\nConclusion: HTTP keep-alive is required to trigger the loss; the number")
    print("of NONE->AES transitions scales it (1 transition ~1.8%, 62 ~17%).")
    print("Fix: -http_persistent 0")

    if not args.keep:
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
