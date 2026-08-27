#!/usr/bin/env python3
"""Simulate Nextory presigned-URL expiry mid-stream and observe ffmpeg's reaction.

Serves a Nextory-shaped playlist (every 6th segment unencrypted, AES-128 otherwise)
over HTTP/1.1. Segments at index >= EXPIRE_AT return HTTP 403, mimicking an
nx-expires timeout part-way through a chapter.

Questions answered:
  1. Does ffmpeg abort, skip, or stall on a 403 segment?
  2. What exit code does it report? (Does MA see an error, or a clean EOF?)
  3. Does ffmpeg re-request the media playlist to get fresh presigned URLs?
  4. Do -reconnect_on_http_error / -xerror change any of it?
"""
# ruff: noqa: T201, S603, PLW1510

from __future__ import annotations

import collections
import http.server
import os
import pathlib
import shutil
import socketserver
import subprocess
import threading

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
WORK = pathlib.Path(__file__).parent / "_work_expiry"
PORT = 8902
N_SEG = 60
EXPIRE_AT = 30
TOTAL_S = 360

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
REQUESTS: collections.Counter[str] = collections.Counter()
LOCK = threading.Lock()


def parse_adts(data: bytes) -> float:
    """Sum ADTS frame durations to get an exact duration in seconds."""
    off, dur, n = 0, 0.0, len(data)
    while off + 7 <= n:
        if data[off] != 0xFF or (data[off + 1] & 0xF0) != 0xF0:
            break
        rate = ADTS_RATES[(data[off + 2] >> 2) & 0x0F]
        flen = ((data[off + 3] & 0x03) << 11) | (data[off + 4] << 3) | (data[off + 5] >> 5)
        if flen < 7 or off + flen > n:
            break
        if rate:
            dur += 1024 / rate
        off += flen
    return dur


def seg_idx(path: str) -> int | None:
    """Extract a segment index from a request path, or None if not a segment."""
    for tag in ("_p", "_e"):
        if tag in path and path.endswith(".aac"):
            try:
                return int(path.rsplit(tag, 1)[1].removesuffix(".aac"))
            except ValueError:
                return None
    return None


class Handler(http.server.SimpleHTTPRequestHandler):
    """Keep-alive server that 403s any segment at or past EXPIRE_AT."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Serve a segment, or 403 it to simulate an expired presigned URL."""
        path = self.path.split("?", 1)[0].lstrip("/")
        with LOCK:
            REQUESTS[path] += 1
        i = seg_idx(path)
        if i is not None and i >= EXPIRE_AT:
            self.send_error(403, "Signature expired")
            return
        super().do_GET()

    def log_message(self, *a):
        """Silence per-request logging."""


def sh(*a: str) -> subprocess.CompletedProcess[str]:
    """Run a command and capture its output."""
    return subprocess.run(a, capture_output=True, text=True)


def build() -> float:
    """Build the playlist and segments. :returns: reachable seconds before expiry."""
    sh(
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc=0.5*sin(2*PI*(200+t*20)*t):d={TOTAL_S}:s=44100",
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
        str(WORK / "source.aac"),
    )
    sh(
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(WORK / "source.aac"),
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        "6",
        "-segment_format",
        "adts",
        str(WORK / "s_%04d.aac"),
    )
    segs = sorted(WORK.glob("s_[0-9][0-9][0-9][0-9].aac"))[:N_SEG]
    (WORK / "key.bin").write_bytes(KEY)
    base = f"http://127.0.0.1:{PORT}"
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:7", "#EXT-X-MEDIA-SEQUENCE:0"]
    good = 0.0
    for i, seg in enumerate(segs):
        raw = seg.read_bytes()
        d = parse_adts(raw)
        if i < EXPIRE_AT:
            good += d
        if i % 6 == 0:
            lines.append("#EXT-X-KEY:METHOD=NONE")
            name = f"seg_p{i:04d}.aac"
            (WORK / name).write_bytes(raw)
        else:
            iv = os.urandom(16)
            p = PKCS7(128).padder()
            e = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
            name = f"seg_e{i:04d}.aac"
            (WORK / name).write_bytes(e.update(p.update(raw) + p.finalize()) + e.finalize())
            lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{base}/key.bin",IV=0x{iv.hex()}')
        lines.append(f"#EXTINF:{d:.3f},")
        lines.append(f"{base}/{name}?nx-expires=1893456000&nx-signature=deadbeef")
    lines.append("#EXT-X-ENDLIST")
    (WORK / "playlist.m3u8").write_text("\n".join(lines) + "\n")
    return good


def run(label: str, good_s: float, extra: list[str]) -> None:
    """Run one ffmpeg variant and report produced audio, exit code and fetch counts."""
    with LOCK:
        REQUESTS.clear()
    out = WORK / ("o_" + "".join(c if c.isalnum() else "_" for c in label) + ".pcm")
    r = sh(
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-allowed_extensions",
        "ALL",
        "-protocol_whitelist",
        "file,hls,crypto,data,http,https,tls,tcp",
        "-http_persistent",
        "0",
        *extra,
        "-i",
        f"http://127.0.0.1:{PORT}/playlist.m3u8",
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        "44100",
        str(out),
    )
    got = out.stat().st_size / 88200 if out.exists() else 0.0
    with LOCK:
        pl_reqs = REQUESTS.get("playlist.m3u8", 0)
        exp_reqs = sum(
            v for k, v in REQUESTS.items() if (i := seg_idx(k)) is not None and i >= EXPIRE_AT
        )
    print(f"\n{label}")
    print(
        f"  audio produced       : {got:7.2f}s  of {TOTAL_S}s "
        f"(reachable before expiry: {good_s:.2f}s)"
    )
    print(
        f"  exit code            : {r.returncode}"
        f"{'   <-- clean EOF, MA sees no error' if r.returncode == 0 else ''}"
    )
    print(f"  playlist fetches     : {pl_reqs}{'   <-- never refreshed' if pl_reqs <= 1 else ''}")
    print(f"  403 segment requests : {exp_reqs}")
    for ln in [x for x in r.stderr.splitlines() if x.strip()][:2]:
        print(f"  stderr: {ln[:105]}")


if __name__ == "__main__":
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    os.chdir(WORK)
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler)
    srv.allow_reuse_address = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        good = build()
        print(f"{N_SEG} segments; URLs return 403 from segment {EXPIRE_AT} onward")
        run("default", good, [])
        run(
            "-reconnect_on_http_error 403",
            good,
            ["-reconnect", "1", "-reconnect_delay_max", "2", "-reconnect_on_http_error", "403"],
        )
        run(
            "-reconnect_on_http_error 4xx",
            good,
            ["-reconnect", "1", "-reconnect_delay_max", "2", "-reconnect_on_http_error", "4xx"],
        )
        run("-xerror", good, ["-xerror"])
    finally:
        srv.shutdown()
