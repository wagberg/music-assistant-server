# Nextory Provider — Architecture

Current-state reference for this provider. For narrower topics see:
- [`SESSION_HANDLING.md`](SESSION_HANDLING.md) — auth/session lifecycle, concurrent-stream
  eviction, and how Music Assistant's own scheduling interacts with it.
- [`STREAMING.md`](STREAMING.md) — HLS structure, the ffmpeg keep-alive bug, and the
  download+decrypt streaming implementation.
- [`../../../scripts/nextory_debug/README.md`](../../../scripts/nextory_debug/README.md) —
  diagnostic scripts for the streaming path.
- [`nextory` client library docs](https://github.com/wagberg/nextory/blob/main/docs/AUTHENTICATION_FLOW.md)
  (checked out locally at `~/opt/nextory` for this fork's maintainer) — the API client this
  provider depends on; token semantics and the concurrent-session/eviction model live there,
  not duplicated here.

## What this provider does

Streams audiobooks from Nextory (a Swedish/Nordic audiobook service). Not an official Music
Assistant provider — this is a personal fork. `manifest.json` declares
`requirements: ["nextory @ git+https://github.com/wagberg/nextory.git@v0.1.0", "cryptography>=43.0.0"]`.

Declared features (`SUPPORTED_FEATURES` in `__init__.py`): `BROWSE`, `LIBRARY_AUDIOBOOKS`,
`RECOMMENDATIONS`, `SEARCH`. No podcasts (Nextory doesn't offer them), no library
add/remove editing, no playlists.

## File layout

| File | Role |
|---|---|
| `__init__.py` | `NextoryProvider(MusicProvider)`: library sync, search, browse, recommendations, streaming, resume position. |
| `setup_flow.py` | Interactive setup: `run_setup(session)`, called by MA's setup-flow engine (see below). |
| `constants.py` | Shared `CONF_*` setup-data keys, imported by both of the above. |
| `manifest.json` | Provider metadata + pip requirements. |
| `icon.svg`, `icon_monochrome.svg` | UI icons. |

## Setup flow

Music Assistant's provider config architecture (as of the 2.10 line) has no module-level
`get_config_entries(mass, instance_id, action, values)` function anymore — that pattern (still
used by some older providers pre-2.10) was removed. Interactive one-time setup input goes through
`providers/<domain>/setup_flow.py: run_setup(session: SetupSession)`
(`music_assistant/models/setup_flow.py`); `Provider.get_config_entries(self)` (an instance method)
is now only for post-setup, non-interactive options. This provider defines no such options — it
has none — so `get_config_entries` is not overridden and the base class default (`()`) applies.

`run_setup` (in `setup_flow.py`) is two steps:

1. **`user` step** — username/password form. Calls `NextoryClient().login()` directly to validate
   before proceeding; on failure, re-shows the form with an inline error.
2. **`profile` step** — fetches the account's profiles and available languages (via the just-
   obtained `login_token`), shows a combined profile + language picker (auto-selected if there's
   only one profile), calls `select_profile()`, then `session.finish()`.

`session.finish()` persists exactly these keys to the provider's `setup_data` (read back at
runtime via `self.get_setup_value(key)`, see `handle_async_init`):

| Key | Source | Notes |
|---|---|---|
| `login_token` | `client.login()` | Long-lived; see `SESSION_HANDLING.md`. |
| `login_key` | profile the user picked | Needed to refresh `profile_token` without re-entering credentials. |
| `profile_token` | `client.select_profile()` | Short-lived; self-refreshes via the `nextory` client, see `SESSION_HANDLING.md`. |
| `language` | picked, or account default | Used for locale-tagged API requests. |

**Deliberately not persisted: username/password.** Once `login_token` + `login_key` are held,
neither is needed again — `login_token` is stable/deterministic and `profile_token` refreshes from
`login_key` alone (see `SESSION_HANDLING.md`). This differs from some other providers in this
codebase (e.g. `storytel`) that persist raw credentials to support a different re-auth pattern;
that pattern is unnecessary here and intentionally not used.

## Streaming

See `STREAMING.md` for the full technical history. Summary: audiobook chapters are HLS with
every 6th segment left unencrypted and the rest AES-128-CBC encrypted. The provider downloads and
decrypts segments in Python rather than letting ffmpeg's native HLS demuxer handle it, because
that demuxer silently drops 17-37% of audio on Nextory's encryption-toggling pattern (root-caused
in `STREAMING.md`; the fix, `-http_persistent 0`, is known but the Python path was left in place
since it's unaffected and switching is optional polish, not a bug fix).

## Known limitations

- No automatic UI-facing distinction yet between "Nextory is down" and "your concurrent-stream
  limit is in use" beyond the error message text (`_handle_nextory_error` in `__init__.py`) — both
  raise `ProviderUnavailableError`, just with different message prefixes.
- No test suite for this provider.
