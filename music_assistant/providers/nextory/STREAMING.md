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
Media playlist contains ~6-second AAC segments:

```
#EXTM3U
#EXT-X-KEY:METHOD=NONE
#EXTINF:6.014,title="..."
https://media.nextory.com/.../uncrypted/.../chapter_01_0.aac?nx-expires=...&nx-signature=...
#EXT-X-KEY:METHOD=AES-128,URI="https://api.nextory.com/reader/encryption_keys/1292151",IV=0x7c948d89...
#EXTINF:5.946,title="..."
https://media.nextory.com/.../crypted/.../chapter_01_1.aac?nx-expires=...&nx-signature=...
...
#EXT-X-ENDLIST
```

- First segment per chapter is unencrypted (`METHOD=NONE`)
- Remaining segments use AES-128-CBC encryption
- Encryption key URL requires auth headers (login token, profile token, device headers)
- Segment URLs are pre-signed (no auth needed) with ~1 hour expiry (`nx-expires`)
- All API endpoints (`api.nextory.com`) require auth headers managed by NextoryClient middlewares

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
2. For each chapter, `_get_hls_segments` fetches the media playlist with auth
3. Parses m3u8 line by line: tracks current key/IV, downloads each segment with auth
4. Decrypts AES-128-CBC segments using `cryptography` library, strips PKCS7 padding
5. Yields raw AAC bytes into an async generator
6. `get_ffmpeg_stream` feeds the generator to ffmpeg via stdin, outputs FLAC
7. MA's outer ffmpeg handles final format conversion for the player

Seeking: skips entire chapters using `start_at`/`duration`, skips segments within
a chapter by accumulating `#EXTINF` durations. Granularity is ~6 seconds.

## Failed Approach: Let ffmpeg Handle HLS Directly

The original approach was to let ffmpeg's built-in HLS demuxer handle everything:

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

**Problem 2: `crypto+https://` decryption failures**

Even with `-allowed_extensions ALL` and the correct key (verified by manual Python decryption),
ffmpeg's `crypto+https://` protocol handler failed to decrypt remote segments:

```
Opening 'crypto+https://media.nextory.com/.../chapter_01_1.aac?...' for reading
Error during demuxing: Invalid data found when processing input
Error decoding AAC frame header.
```

The same segments decrypted correctly with:
- Python `cryptography` library (AES-128-CBC + PKCS7 unpad) → valid AAC
- `openssl aes-128-cbc -d` → valid AAC
- ffmpeg with local files (`crypto:/tmp/seg_1.aac`) → played fine

The failure is specific to `crypto+https://` with pre-signed URLs. Possibly an ffmpeg bug
with the crypto protocol handler when combined with HTTPS URLs containing query parameters.
A 5-segment test m3u8 with `data:` URI keys and remote segments DID work (23.82s),
but the full 369-segment m3u8 stopped at 6.01s (one segment). The exact cause is unclear.

**Problem 3: MA's default `-protocol_whitelist` override**

MA's `get_ffmpeg_args` already includes a comprehensive protocol whitelist:
```
file,hls,http,https,tcp,tls,crypto,pipe,data,fd,rtp,udp,concat
```

Our initial code added a custom `-protocol_whitelist` in `extra_input_args` that was
more restrictive (missing `pipe`, `hls`, etc.), which overrode the default and broke
ffmpeg's ability to read from stdin and parse HLS.

## Revisiting the ffmpeg HLS Approach

### ✅ Solution: `-headers` option (confirmed working)

Passing auth headers via ffmpeg's `-headers` flag works for the full pipeline:

```bash
ffmpeg -v error \
  -headers $'X-Application-Id: 200\r\nX-App-Version: 2026.01.3\r\nX-Locale: sv_SE\r\nX-Model: Personal Computer\r\nX-Device-Id: q8lDJOBAMKizfHKAnZ0ElA\r\nX-Login-Token: <token>\r\nX-Profile-Token: <token>\r\n' \
  -protocol_whitelist file,https,tls,tcp,crypto,data \
  -allowed_extensions ALL \
  -i https://api.nextory.com/reader/books/{format_id}/chapters/chapter_1/master_playlist?quality=low \
  -c copy output.aac
```

ffmpeg sends the custom headers on ALL HTTP requests, which means:
- Master playlist fetch → auth headers sent → works (needs auth)
- Media playlist fetch → auth headers sent → works (needs auth)
- Encryption key fetch → auth headers sent → works (needs auth)
- Segment fetch → auth headers sent → harmless (pre-signed URLs ignore extra headers)

This lets ffmpeg handle the entire HLS pipeline natively: playlist parsing, segment
downloading, AES-128 decryption, and seeking.

### Switching from Python download+decrypt to ffmpeg HLS

To switch the implementation:
1. Build the headers string from the NextoryClient's auth state
2. Pass master playlist URL directly to ffmpeg with `-headers` and `-allowed_extensions ALL`
3. For multi-chapter: either use ffmpeg concat or run one ffmpeg per chapter

Advantages over current Python approach:
- No `cryptography` library dependency
- ffmpeg handles seeking natively (no segment-level skip logic)
- Less code, fewer moving parts
- Potentially better performance (ffmpeg's optimized C code vs Python)

Open questions:
- Multi-chapter concatenation: how to chain 17 HLS streams?
- Header refresh: if profile token expires mid-stream, ffmpeg can't re-auth
- MA integration: use `StreamType.HLS` or keep `StreamType.CUSTOM` with ffmpeg subprocess?

### Other approaches considered (not needed)

1. **`data:` base64 key URIs** — blocked by ffmpeg's extension check, fixable with
   `-allowed_extensions ALL` but `crypto+https://` still had issues with full playlists.

2. **`data:text/plain;hex` key URIs** — works with `-allowed_extensions ALL`. The key
   is embedded as hex directly in the m3u8: `URI="data:text/plain;hex,91b274..."`.
   Tested successfully but crashed ~20% through a full chapter, likely due to pre-signed
   segment URLs expiring (~1 hour TTL). Not viable for long chapters at real-time speed.

3. **Key as temp file** — avoids extension check but same `crypto+https://` issue.

4. **Local HTTP proxy** — overkill now that `-headers` works.
