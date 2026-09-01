# Nextory streaming debug tools

Diagnostic scripts for the Nextory provider's HLS streaming path. These exist to
document and re-verify a specific ffmpeg bug that silently destroyed 17–37% of
every audiobook chapter, and to make it cheap to re-test if the provider ever
switches from the Python decrypt path to ffmpeg-native HLS.

Full analysis lives in
[`music_assistant/providers/nextory/STREAMING.md`](../../music_assistant/providers/nextory/STREAMING.md).

## The bug, in one paragraph

Nextory leaves **every 6th segment unencrypted** (`#EXT-X-KEY:METHOD=NONE`) and
AES-128-CBC encrypts the rest, so a chapter playlist toggles between `NONE` and
`AES-128` about 62 times. ffmpeg reuses its keep-alive HTTP connection across those
transitions and corrupts the first one or two *encrypted* segments after each one.
The affected segments fail to decode and are dropped, costing 17–37% of the chapter.
The fix is `-http_persistent 0`.

The provider currently uses the Python download+decrypt path, which is unaffected.

### Two failure modes, two exit codes

These are distinct and it matters, because MA's `get_ffmpeg_stream` only raises
`AudioError` when the return code is non-zero:

| Failure | Loss | Exit code | MA sees |
|---|---|---|---|
| Keep-alive corruption (this bug) | 17–37% | **234** | an error — detectable |
| Presigned URL expiry mid-chapter (HTTP 403) | rest of chapter | **0** | a clean EOF — **silent truncation** |

So the keep-alive bug is loud once you look at the return code, but URL expiry is
silent: ffmpeg skips every expired segment, never re-fetches the playlist to get
fresh signatures, and reports success. `-reconnect_on_http_error` and `-xerror` do
not change this (see `expiry_test.py`). Any ffmpeg-native implementation must
therefore compare produced duration against the expected chapter duration rather
than trusting the exit code.

## Scripts

| Script | Credentials | What it does |
|---|---|---|
| `repro_keepalive.py` | no | Reproduces the bug synthetically and prints the isolation matrix proving keep-alive is the trigger. Start here. |
| `expiry_test.py` | no | Simulates presigned-URL expiry (HTTP 403) mid-chapter and shows ffmpeg cannot recover and still exits 0. |
| `diagnose.py` | **yes** | Live analysis of a real book: metadata sweep, or per-segment ADTS frame accounting vs `#EXTINF`. |
| `verify_fix.py` | **yes** | Runs a real chapter with and without `-http_persistent 0` and diffs the durations. |
| `_common.py` | — | Shared helpers: ADTS frame walker, playlist parser, credential loading, client setup. |

Run everything with the project venv from the repo root:

```bash
.venv/bin/python scripts/nextory_debug/repro_keepalive.py
.venv/bin/python scripts/nextory_debug/expiry_test.py
.venv/bin/python scripts/nextory_debug/diagnose.py --scan
.venv/bin/python scripts/nextory_debug/diagnose.py --chapter 1
.venv/bin/python scripts/nextory_debug/verify_fix.py 1 2
```

`repro_keepalive.py` and `expiry_test.py` need no account and are the ones to reach
for first — they bind a local HTTP server on ports 8901/8902.

## Expected output

`repro_keepalive.py` (ffmpeg 7.1.1):

```
file://          single plain segment        ref= 360.00s hls= 360.00s  clean
file://          every 6th plain             ref= 360.00s hls= 360.00s  clean
http/1.0 no-KA   single plain segment        ref= 360.00s hls= 360.00s  clean
http/1.0 no-KA   every 6th plain             ref= 360.00s hls= 360.00s  clean
http/1.1 KEEPALIVE  single plain segment     ref= 360.00s hls= 353.66s  *** LOST 6.34s (1.8%) ***
http/1.1 KEEPALIVE  every 6th plain  <-- BUG ref= 360.00s hls= 297.38s  *** LOST 62.62s (17.4%) ***
http/1.1 KEEPALIVE  every 6th + FIX          ref= 360.00s hls= 360.00s  clean
```

Keep-alive is the necessary condition; the number of `NONE`→`AES` transitions sets
the magnitude. One transition costs ~1 segment, 62 transitions cost 17%+.

`verify_fix.py 1 2` against book 86834:

```
chapter 1: default 1826.04s (-17.3%, 453 errors) | fixed 2208.31s (0 errors)
chapter 2: default  993.50s (-36.9%, 235 errors) | fixed 1574.01s (0 errors)
```

## Credentials

`diagnose.py` and `verify_fix.py` read credentials from the first of these that
exists, and **never print values** — only key names:

1. `~/.config/nextory/profile.yaml`
2. `~/.config/nextory/profile.json`
3. `~/.config/nextory-ma-debug/creds.json`

Provide either form:

```yaml
login_token: "..."
login_key: "..."
profile_token: "..."
```

```json
{"username": "...", "password": "..."}
```

Prefer the token form. A fresh username/password login consumes a **profile session
slot** on the account (the client raises `MaxProfileSessionsError` when exhausted),
which can disturb a running MA instance. A stale `profile_token` is refreshed
automatically when `login_key` is present. Full detail on the concurrent-stream/eviction
model and how it interacts with a running provider:
[`music_assistant/providers/nextory/SESSION_HANDLING.md`](../../music_assistant/providers/nextory/SESSION_HANDLING.md).

## Why measurements use ADTS frame walking

`ffprobe -show_entries format=duration` is a bitrate *estimate* on raw ADTS and can
be several seconds wrong — it reported 119.09s and 126.82s for files that are really
120.02s and 120.05s. `_common.parse_adts` walks ADTS frame headers and sums
`1024 / sample_rate` per frame, which is exact. Anything measuring audio loss here
must use it rather than ffprobe.

## Security note

Generated artifacts are gitignored, and they must stay that way:

- ffmpeg logs captured with `-headers` contain live `X-Login-Token` and
  `X-Profile-Token` values
- saved `.m3u8` playlists contain presigned `nx-signature` segment URLs

Both are credentials. Do not attach raw logs or playlists to issues without
redacting them.
