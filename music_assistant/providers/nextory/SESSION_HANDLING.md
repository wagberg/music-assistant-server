# Session Handling: Music Assistant × Nextory's Concurrent-Stream Model

How this provider's calls into Nextory interact with account-level concurrent-stream limits and
profile eviction. The underlying Nextory mechanism (tiers, eviction, token characteristics, error
codes) is documented in the `nextory` client library, not here —
[`docs/AUTHENTICATION_FLOW.md`](https://github.com/wagberg/nextory/blob/main/docs/AUTHENTICATION_FLOW.md)
(`~/opt/nextory/docs/AUTHENTICATION_FLOW.md` for this fork's maintainer). Read that first; this
document only covers what's specific to running as a Music Assistant provider.

## Which calls need `ProfileAuth`, and which don't

Everything this provider does through `self._client` (`NextoryClient`) after `handle_async_init`
needs `ProfileAuth` (i.e. a valid `profile_token`) — search, browse, recommendations, library
sync, fetching audiobook/chapter metadata, resume-position read/write. **Exception**: the actual
audio segment bytes fetched in `get_audio_stream` are presigned URLs that need no auth at all (see
the client library's "No Authentication (Presigned Media)" section) — only the *playlist* and
*encryption key* fetches that precede them need `ProfileAuth`.

## User-visible consequence of eviction

Because segment delivery is unauthenticated, a chapter **already being downloaded** is unaffected
by `profile_token` becoming invalid mid-playback (e.g. because the Nextory phone app authorized
the same profile and evicted this session — see the client docs' "Concurrent Sessions" section).
What actually breaks, and when:

| Action | Needs `ProfileAuth` | Effect of a stale `profile_token` |
|---|---|---|
| Continue current chapter's audio | No (presigned) | Unaffected |
| Move to next chapter (`get_stream_details`) | Yes | Auto-refreshes via `login_key` (see below); user sees at most a brief delay |
| Report playback position (`on_played`) | Yes | Same — auto-refreshes |
| Seek within the current chapter | No, if already-downloaded segments cover it | Unaffected |

So the practical symptom of "the phone app is using this profile" is *not* interrupted playback —
it's a slightly slower chapter transition or position update, self-healing via the `nextory`
client's built-in retry-on-2002 (`_refresh_profile_token`, using the persisted `login_key`, no
credentials or user interaction involved).

## How often this provider actually touches the API in the background

Outside of direct user interaction (browsing, searching, playback), MA triggers requests through
this provider at exactly two points:

1. **Provider load** (`handle_async_init`) — once per Music Assistant startup, and once per
   reconfigure/reload. Calls `get_account()`, `get_profiles()`, `get_libraries()`, and paginated
   `get_library()` to seed `_ongoing_product_ids`.
2. **Scheduled library sync** — every 12 hours for `AUDIOBOOK`
   (`Provider.get_default_library_sync_schedule`, fixed and non-overridable by provider code),
   anchored to the persisted last-run time. A restart or reconfigure does **not** force an
   immediate resync — only the very first sync after adding the provider instance fires quickly
   (10s later).

Nothing here proactively re-authorizes the profile on a schedule; the `nextory` client's built-in
refresh is reactive (only on an actual 2002), which matches the client library's recommendation to
avoid gratuitous eviction risk. There is no code path in this provider that calls
`select_profile()` outside of `setup_flow.py` — at runtime, only the client's own automatic
refresh does so.

## Error mapping and Music Assistant's own retry behavior

`_handle_nextory_error` (`__init__.py`) maps `nextory` exceptions to Music Assistant exceptions:

| `nextory` exception | MA exception raised | MA's `load_provider` auto-retries on this? |
|---|---|---|
| `ExpiredLoginTokenError`, `InvalidAuthTokenError` | `LoginFailed` | **No** — `mass.py` explicitly excludes `LoginFailed` from its retry-on-load-failure logic; the instance sits failed until the user reconfigures. |
| `MaxProfileSessionsError` | `ProviderUnavailableError` ("Nextory concurrent stream limit reached: ...") | **Yes** — see below. |
| `ExpiredProfileTokenError` (only reachable if it recurs immediately *after* the client's own built-in retry) | `ProviderUnavailableError` ("Nextory profile session was taken by another device: ...") | **Yes** — see below. |
| Any other `NextoryApiError` | `ProviderUnavailableError` | Yes. |

**The retry**: for anything raising `ProviderUnavailableError` (or any `MusicAssistantError`
subtype other than `LoginFailed`/`AuthenticationRequired`/`AuthenticationFailed`/`InvalidToken`),
`mass.py: load_provider` schedules an automatic retry of the *entire provider load* every
**120 seconds**, indefinitely, with no backoff. Concretely: if MA starts up while this account's
other profiles already hold both/all concurrent-stream slots, `handle_async_init` fails with
`MaxProfileSessionsError` → `ProviderUnavailableError`, and MA will keep retrying the full load
(re-running `get_account()` etc., which itself attempts to reuse/refresh the persisted
`profile_token`) every 2 minutes until a slot frees up. This is not currently tuned specifically
for this scenario — it's Music Assistant's generic provider-load retry behavior, applied here.

**Known related gap** (documented in the client library, repeated here because it changes MA's
retry behavior specifically): `_refresh_profile_token()` in the `nextory` client can raise a bare
`ValueError` instead of a typed `NextoryApiError` if the refresh call itself fails oddly. A bare
`ValueError` is not a `MusicAssistantError`, so it does **not** qualify for the 120-second retry
above — that failure mode gives up immediately instead of retrying, unlike the "normal"
`MaxProfileSessionsError` case.
