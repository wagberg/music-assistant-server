# Nextory Streaming Architecture

## HLS Structure

Each audiobook has multiple chapters. Each chapter is an HLS stream:

```
AudioPackage
 └─ files[] (one per chapter)
     ├─ uri: .../chapters/chapter_01/master_playlist?quality=low
     ├─ start_at: 0        (ms, absolute position in book)
     ├─ duration: 2208371  (ms)
     └─ end_at: 2208371    (ms)
```

Master playlist → media playlist (replace "master" with "media" in URL).
Media playlist contains ~6-second AAC segments. **Every 6th segment is
unencrypted**, the rest are AES-128-CBC:

```
#EXTM3U
#EXT-X-KEY:METHOD=NONE
#EXTINF:6.014,title="..."
https://media.nextory.com/.../uncrypted/.../chapter_01_0.aac?nx-expires=...&nx-signature=...
#EXT-X-KEY:METHOD=AES-128,URI="https://api.nextory.com/reader/encryption_keys/1292151",IV=0x7c948d89...
#EXTINF:5.946,title="..."
https://media.nextory.com/.../crypted/.../chapter_01_1.aac?nx-expires=...&nx-signature=...
...                          (segments 2-5 also AES-128, each with its own IV)
#EXT-X-KEY:METHOD=NONE       ← segment 6 unencrypted again
#EXTINF:6.014,title="..."
https://media.nextory.com/.../uncrypted/.../chapter_01_6.aac?...
...
#EXT-X-ENDLIST
```

- Segments at index `n % 6 == 0` are unencrypted (`METHOD=NONE`), served from an
  `/uncrypted/` path. All others use AES-128-CBC from a `/crypted/` path.
  Measured on chapter 1 of book 86834: 369 segments = 62 unencrypted + 307 encrypted,
  so the playlist toggles between `METHOD=NONE` and `AES-128` **62 times per chapter**.
  This repeated toggling is what breaks ffmpeg's native HLS demuxer — see
  "Root cause of the ffmpeg HLS failure" below.
- Each encrypted segment carries its **own IV** in its `#EXT-X-KEY` line. The key URI
  is the same throughout a chapter (ffmpeg fetches it once and caches it).
- Each encrypted segment is independently PKCS7-padded to a 16-byte boundary.
- Encryption key URL requires auth headers (login token, profile token, device headers)
- Segment URLs are pre-signed (no auth needed) with ~1 hour expiry (`nx-expires`)
- All API endpoints (`api.nextory.com`) require auth headers managed by NextoryClient middlewares
- Segments are packed ADTS AAC-LC, 44100 Hz (not HE-AAC, despite `quality=low`)

## Current Approach: Download + Decrypt in Python

```
NextoryClient                    ffmpeg (stdin → stdout)
     │                                │
     ├─ GET media_playlist ───┐       │
     │                        │       │
     ├─ GET encryption_key    │       │
     │                        │       │
     ├─ GET segment_0 ────────┼──► raw AAC bytes ──► stdin
     ├─ GET segment_1 ────────┤       │                │
     │   └─ AES-128 decrypt   │       │           ┌────┘
     ├─ GET segment_2 ────────┤       │           │
     │   └─ AES-128 decrypt   │       │      FLAC output ──► MA outer ffmpeg
     │   ...                  │       │
     └─ (next chapter)        │       │
```

1. `get_audio_stream` fetches the audio package (chapter list)
2. For each chapter, `_fetch_chapter_playlist` fetches the media playlist with auth
   (retrying once with a refreshed profile token on error code 2002) and fetches the
   encryption key
3. `_parse_playlist` walks the m3u8 tracking the current key/IV per segment and
   resolves the seek position
4. Decrypts AES-128-CBC segments using `cryptography` library, strips PKCS7 padding
5. Yields raw AAC bytes into an async generator
6. `get_ffmpeg_stream` feeds the generator to ffmpeg via stdin, outputs FLAC
7. MA's outer ffmpeg handles final format conversion for the player

Seeking: skips entire chapters using `start_at`/`duration`, skips segments within
a chapter by accumulating `#EXTINF` durations. Granularity is ~6 seconds.

## Earlier Attempt: Let ffmpeg Handle HLS Directly

An earlier approach let ffmpeg's built-in HLS demuxer handle everything. It hit three
problems, all now resolved:

1. Fetch media playlist with auth
2. Replace encryption key URLs with inline `data:` base64 URIs (since ffmpeg can't send auth headers)
3. Write resolved m3u8 to a temp file
4. Pass temp file path to ffmpeg as input

```
#EXT-X-KEY:METHOD=AES-128,URI="data:application/octet-stream;base64,S1ZZngOJ06FKOKDKmcfJyw==",IV=0x...
```

### Why It Failed

**Problem 1: ffmpeg blocks `data:` URIs for key files**

ffmpeg 7.1.1's HLS demuxer checks file extensions for security. The `data:application/octet-stream;base64,...`
URI has no recognized multimedia extension, so ffmpeg blocks it:

```
Filename extension of 'data:application/octet-stream;base64,...' is not a common multimedia extension,
blocked for security reasons.
If you wish to override this adjust allowed_extensions, you can set it to 'ALL' to allow all
Unable to open key file data:application/octet-stream;base64,...
```

Fix: `-allowed_extensions ALL` as input arg.

**Problem 2: silent audio loss — segments decode to ~1s, playback skips ahead by minutes**

With `-allowed_extensions ALL` and the correct key, ffmpeg downloaded every segment
but silently discarded 17–37% of the audio. Symptom: some segments produced ~1s
instead of ~6s and the book jumped forward by several minutes.

```
Opening 'crypto+https://media.nextory.com/.../chapter_01_1.aac?...' for reading
Error during demuxing: Invalid data found when processing input
Error decoding AAC frame header.
```

This was diagnosed and **solved** — see "Root cause of the ffmpeg HLS failure" below.
The fix is `-http_persistent 0`. It is **not** specific to `crypto+https://`,
pre-signed URLs, or query parameters, and it is not a padding or codec issue;
all of those were tested and cleared.

**Problem 3: MA's default `-protocol_whitelist` override**

MA's `get_ffmpeg_args` already includes a comprehensive protocol whitelist:
```
file,hls,http,https,tcp,tls,crypto,pipe,data,fd,rtp,udp,concat
```

Our initial code added a custom `-protocol_whitelist` in `extra_input_args` that was
more restrictive (missing `pipe`, `hls`, etc.), which overrode the default and broke
ffmpeg's ability to read from stdin and parse HLS.

## Root cause of the ffmpeg HLS failure

**ffmpeg reuses a keep-alive HTTP connection across `METHOD=NONE` → `AES-128`
transitions and corrupts the first one to two encrypted segments after each one.**

When the HLS demuxer switches between the plain `https://` handler and the
`crypto+https://` wrapper on a reused connection, the crypto layer begins on stale
connection state. The affected segments fail to decode and their audio is dropped
entirely, so playback jumps ahead. Because Nextory toggles `METHOD=NONE` 62 times
per chapter, the loss accumulates into minutes.

### Fix

```
-http_persistent 0
```

Disables HTTP connection reuse. Costs one TCP+TLS handshake per segment
(369 in chapter 1), which is acceptable.

### Evidence

Errors land **exclusively** on cycle positions 1 and 2 — the first two encrypted
segments after each return from `METHOD=NONE` (chapter 1, book 86834):

| Position in 6-cycle | Type | Segments with decode errors |
|---|---|---|
| 0 | unencrypted | 0 / 62 |
| 1 | encrypted | 58 / 62 |
| 2 | encrypted | 50 / 62 |
| 3, 4, 5 | encrypted | 0 / 61 each |

Verified against the real book (`Harry Potter och De Vises Sten`, format 10057685):

| Chapter | ffmpeg default | With `-http_persistent 0` | Expected |
|---|---|---|---|
| 1 | 1826.04s, 453 errors, −17.3% | **2208.31s, 0 errors** | 2208.31s |
| 2 | 993.50s, 235 errors, −36.9% | **1574.01s, 0 errors** | 1574.05s |

The source data is not at fault. For all 369 segments of chapter 1: every segment
decrypts cleanly, PKCS7 padding is valid on every one, each segment's ADTS frame
count matches its `#EXTINF` to within 0.000s, a single 44100 Hz rate throughout,
no trailing junk, no sync loss. ffmpeg fetches all 369 segments and classifies
encrypted vs plain with 100% accuracy (307/62, zero disagreement with the playlist) —
it downloads everything correctly and then throws part of it away.

### Isolation matrix

HTTP keep-alive is the **necessary condition**; the number of NONE→AES transitions
sets the magnitude. That is why local-file tests and short single-transition tests
both looked healthy. Reproduce with `scripts/nextory_debug/repro_keepalive.py`
(no credentials needed):

| Transport | Pattern | Result |
|---|---|---|
| `file://` | single plain segment | clean |
| `file://` | 1-in-6 interleave | clean |
| `http://` HTTP/1.0 (no keep-alive) | single plain segment | clean |
| `http://` HTTP/1.0 (no keep-alive) | 1-in-6 interleave | clean |
| `http://` HTTP/1.1 (keep-alive) | single plain segment | −6.34s (1.8%) |
| `http://` HTTP/1.1 (keep-alive) | 1-in-6 interleave | **−62.62s (17.4%)** |
| `http://` HTTP/1.1 + `-http_persistent 0` | 1-in-6 interleave | clean |

One transition costs ~1 segment; Nextory's 62 transitions per chapter cost 17%+.
The synthetic 1-in-6 figure (17.4%) matches real chapter 1 (17.3%).

### Hypotheses tested and ruled out

Each of these was tested directly and produced byte-identical output, so none is
the cause. Recorded to stop them being re-investigated:

- PKCS7 padding not stripped by ffmpeg
- The `METHOD=NONE` → `AES-128` transition *on its own* (needs keep-alive too)
- Per-segment IV handling
- Codec profile — tested AAC-LC and HE-AAC / HE-AAC v2, mono and stereo,
  22.05 kHz and 44.1 kHz (the real content is AAC-LC 44.1 kHz)
- Pre-signed URL query parameters
- `crypto+file://` and `crypto+http://` transports as such
- Nextory metadata being wrong: all 17 chapters' `#EXTINF` sums match the audio
  package durations exactly (34814.8s both ways)
- MA's injected `-probesize` / `-analyzeduration` / `-reconnect*` args — the bug
  reproduces with plain ffmpeg CLI, so MA's arg construction is not involved

## Revisiting the ffmpeg HLS Approach

### ✅ Solution: `-headers` option (confirmed working)

Passing auth headers via ffmpeg's `-headers` flag works for the full pipeline:

```bash
ffmpeg -v error \
  -headers $'X-Application-Id: 200\r\nX-App-Version: 2026.01.3\r\nX-Locale: sv_SE\r\nX-Model: Personal Computer\r\nX-Device-Id: q8lDJOBAMKizfHKAnZ0ElA\r\nX-Login-Token: <token>\r\nX-Profile-Token: <token>\r\n' \
  -protocol_whitelist file,https,tls,tcp,crypto,data \
  -allowed_extensions ALL \
  -http_persistent 0 \
  -i https://api.nextory.com/reader/books/{format_id}/chapters/chapter_1/master_playlist?quality=low \
  -c copy output.aac
```

`-http_persistent 0` is **required** — without it ffmpeg silently discards 17–37%
of the audio. See "Root cause of the ffmpeg HLS failure" above.

ffmpeg sends the custom headers on ALL HTTP requests, which means:
- Master playlist fetch → auth headers sent → works (needs auth)
- Media playlist fetch → auth headers sent → works (needs auth)
- Encryption key fetch → auth headers sent → works (needs auth)
- Segment fetch → auth headers sent → harmless (pre-signed URLs ignore extra headers)

This lets ffmpeg handle the entire HLS pipeline natively: playlist parsing, segment
downloading, AES-128 decryption, and seeking.

### Switching from Python download+decrypt to ffmpeg HLS

Now viable, since the audio-loss bug is understood and fixable. To switch:

1. Build the headers string from the NextoryClient's auth state
2. Pass master playlist URL directly to ffmpeg with `-headers`,
   `-allowed_extensions ALL` and **`-http_persistent 0`**
3. For multi-chapter: either use ffmpeg concat or run one ffmpeg per chapter

Advantages over current Python approach:
- No `cryptography` library dependency
- ffmpeg handles seeking natively (no segment-level skip logic)
- Less code, fewer moving parts
- Potentially better performance (ffmpeg's optimized C code vs Python)

Remaining tradeoffs and open questions:
- `-http_persistent 0` forces a fresh TCP+TLS handshake per segment (369 in chapter 1)
- Multi-chapter concatenation: how to chain 17 HLS streams?
- Header refresh: if the profile token expires mid-stream, ffmpeg can't re-auth.
  The Python path handles this today by retrying with a refreshed profile token.
- Segment URL expiry: pre-signed URLs last ~1 hour. The Python path detects HTTP 403
  and re-fetches the playlist; ffmpeg cannot, and `-reconnect_on_http_error` does not
  include 403 by default.
- MA integration: use `StreamType.HLS` or keep `StreamType.CUSTOM` with ffmpeg subprocess?

The Python download+decrypt path is unaffected by this bug, so there is no urgency
to switch. It is now a genuine design choice rather than a blocked option.

### Implication for Music Assistant generally

Any MA provider streaming encrypted HLS with interleaved unencrypted segments will
hit this same silent audio loss. A conditional `-http_persistent 0` for HLS inputs
in `get_ffmpeg_args` would fix it for all of them.

## Recovery limits of the ffmpeg-native path

Measured with `scripts/nextory_debug/expiry_test.py`, which serves a Nextory-shaped
playlist and returns HTTP 403 for segments past a cutoff.

### Profile token expiry — recoverable, no gap

In the real 369-segment chapter, ffmpeg made exactly **1 key fetch and 1 playlist
fetch**. Everything needing auth happens at process start; segments are presigned
and need no headers. A profile token expiring mid-chapter therefore does not affect
the chapter in flight. Running one ffmpeg per chapter and refreshing the token
before each spawn handles this completely.

### Segment URL expiry — ffmpeg cannot recover, and fails silently

All variants behaved identically when segments returned 403 part-way through:

| Variant | Audio produced | Playlist fetches | Exit code |
|---|---|---|---|
| default | 180.00s of 360s | 1 (never refreshed) | **0** |
| `-reconnect_on_http_error 403` | 180.00s | 1 | **0** |
| `-reconnect_on_http_error 4xx` | 180.00s | 1 | **0** |
| `-xerror` | 180.00s | 1 | **0** |

ffmpeg never re-fetches the media playlist — it is VOD (`#EXT-X-ENDLIST`), so it has
no reason to — and therefore can never obtain fresh presigned URLs. Retrying is
futile because a 403 on an expired signature is permanent for that URL.

The hazard is the exit code. **`0` even with `-xerror`**, so MA's `get_ffmpeg_stream`
(which raises only when `returncode not in (None, 0)`) would treat a half-lost
chapter as a completed track. Note this differs from the keep-alive bug, which exits
`234` and *is* detectable.

Presigned TTL is ~1 hour; the longest chapter here is 3174s (52.9 min), so
uninterrupted playback just fits, but any pause or stall exceeds it.

### Resuming is possible without silence

`-ss` on an encrypted HLS playlist seeks to the segment boundary at or before the
target, so a respawn produces **overlap, never a gap**:

| Seek | Produced | Exact would be |
|---|---|---|
| `-ss 60` | 305.99s | 300.0s |
| `-ss 180` | 185.99s | 180.0s |
| `-ss 186.5` | 173.99s | 173.5s |

So a supervising implementation can recover seamlessly: compare produced duration
against the chapter duration, and on a shortfall re-fetch the playlist and respawn
with `-ss <offset>`, trimming the known overlap. A local HTTP proxy that re-signs
URLs would also work with zero gap, but reintroduces most of the machinery the
Python path already has.

### Other approaches considered (not needed)

1. **`data:` base64 key URIs** — blocked by ffmpeg's extension check, fixable with
   `-allowed_extensions ALL`. Not needed now that `-headers` works.

2. **`data:text/plain;hex` key URIs** — works with `-allowed_extensions ALL`. The key
   is embedded as hex directly in the m3u8: `URI="data:text/plain;hex,91b274..."`.
   Tested successfully but appeared to crash ~20% through a full chapter. That was
   almost certainly this same keep-alive bug rather than segment URL expiry as
   originally assumed.

3. **Key as temp file** — works, but unnecessary; `-headers` lets ffmpeg fetch the
   real key URL directly.

4. **Local HTTP proxy** — overkill now that `-headers` works.
