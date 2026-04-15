"""Nextory provider for Music Assistant."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, NoReturn, cast

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    UniqueList,
)
from music_assistant_models.media_items.metadata import MediaItemChapter
from music_assistant_models.streamdetails import StreamDetails
from nextory import NextoryClient
from nextory.exceptions import (
    ExpiredLoginTokenError,
    InvalidAuthTokenError,
    MaxProfileSessionsError,
    NextoryApiError,
)
from nextory.models import FormatResponse, FormatState, FormatType, LibraryListType, ProductResponse

from music_assistant.helpers.process import AsyncProcess
from music_assistant.models.music_provider import MusicProvider

STREAM_TIMEOUT = aiohttp.ClientTimeout(total=30)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_LOGIN_TOKEN = "login_token"
CONF_LOGIN_KEY = "login_key"
CONF_PROFILE_TOKEN = "profile_token"
CONF_LANGUAGE = "language"
CONF_ACTION_AUTH = "authenticate"
CONF_ACTION_SELECT_PROFILE = "select_profile"

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SEARCH,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider with given configuration."""
    return NextoryProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    if values is None:
        values = {}

    login_token = cast("str | None", values.get(CONF_LOGIN_TOKEN))
    profile_token = cast("str | None", values.get(CONF_PROFILE_TOKEN))

    # Handle authentication action
    if action == CONF_ACTION_AUTH:
        username = cast("str", values.get(CONF_USERNAME, ""))
        password = cast("str", values.get(CONF_PASSWORD, ""))
        if not username or not password:
            raise LoginFailed("Username and password are required")

        async with NextoryClient() as client:
            login_token = await client.login(username, password)
            values[CONF_LOGIN_TOKEN] = login_token

    # Build profile and language options after login
    profile_options: list[ConfigValueOption] = []
    language_options: list[ConfigValueOption] = []
    default_language = "en"
    if login_token and not profile_token:
        async with NextoryClient(login_token=login_token) as client:
            profiles_resp = await client.get_profiles()
            for profile in profiles_resp.profiles:
                profile_options.append(ConfigValueOption(profile.name, profile.login_key))

            # Fetch account country and available languages
            try:
                lang_names = {
                    "sv": "Svenska",
                    "en": "English",
                    "fi": "Suomi",
                    "da": "Dansk",
                    "nb": "Norsk",
                    "de": "Deutsch",
                    "nl": "Nederlands",
                    "es": "Español",
                    "fr": "Français",
                    "it": "Italiano",
                    "ar": "العربية",
                }
                account = await client.get_account()
                text = await client._request("GET", f"{client._base_url}/user/v1.1/markets")
                for m in json.loads(text):
                    if m["country_code"] == account.country:
                        default_language = m["primary_languages"][0]
                        for lang in m["allowed_languages"]:
                            label = lang_names.get(lang, lang)
                            language_options.append(ConfigValueOption(label, lang))
                        break
            except Exception:  # noqa: S110
                pass

    # Handle profile selection action
    login_key = cast("str | None", values.get(CONF_LOGIN_KEY))
    if action == CONF_ACTION_SELECT_PROFILE and login_token and login_key and not profile_token:
        async with NextoryClient(login_token=login_token) as client:
            profile_token = await client.select_profile(login_key)
            values[CONF_PROFILE_TOKEN] = profile_token

    is_authenticated = bool(profile_token)
    needs_profile = bool(login_token and not profile_token)

    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=not is_authenticated and not needs_profile,
            hidden=is_authenticated or needs_profile,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=not is_authenticated and not needs_profile,
            hidden=is_authenticated or needs_profile,
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH,
            type=ConfigEntryType.ACTION,
            label="Authenticate",
            action=CONF_ACTION_AUTH,
            hidden=is_authenticated or needs_profile,
        ),
        ConfigEntry(
            key=CONF_LOGIN_KEY,
            type=ConfigEntryType.STRING,
            label="Select Profile",
            required=needs_profile,
            hidden=not needs_profile,
            options=profile_options if profile_options else [],
            value=login_key,
        ),
        ConfigEntry(
            key=CONF_LANGUAGE,
            type=ConfigEntryType.STRING,
            label="Language",
            description="Language for content labels and categories",
            required=needs_profile,
            hidden=is_authenticated or not needs_profile,
            options=language_options if language_options else [],
            default_value=default_language,
        ),
        ConfigEntry(
            key=CONF_ACTION_SELECT_PROFILE,
            type=ConfigEntryType.ACTION,
            label="Confirm Profile",
            action=CONF_ACTION_SELECT_PROFILE,
            hidden=not needs_profile,
        ),
        ConfigEntry(
            key=CONF_LOGIN_TOKEN,
            type=ConfigEntryType.STRING,
            label="Login Token",
            hidden=True,
            value=login_token,
        ),
        ConfigEntry(
            key=CONF_PROFILE_TOKEN,
            type=ConfigEntryType.STRING,
            label="Profile Token",
            hidden=True,
            value=profile_token,
        ),
    )


class NextoryProvider(MusicProvider):
    """Nextory Music Provider."""

    _client: NextoryClient
    _profile_id: int
    _profile_name: str | None = None
    _ongoing_list_id: str | None
    _ongoing_product_ids: set[int]
    _categories_cache: list[BrowseFolder] | None = None
    _categories_cache_time: float = 0

    async def handle_async_init(self) -> None:
        """Handle async initialization."""
        login_token = cast("str", self.config.get_value(CONF_LOGIN_TOKEN))
        profile_token = cast("str", self.config.get_value(CONF_PROFILE_TOKEN))
        login_key = cast("str", self.config.get_value(CONF_LOGIN_KEY))

        if not login_token or not profile_token:
            raise LoginFailed("Not authenticated with Nextory")

        self._client = NextoryClient(
            login_token=login_token,
            login_key=login_key,
            profile_token=profile_token,
            session=self.mass.http_session,
        )

        try:
            # Get country from account, language from config
            account = await self._client.get_account()
            self._client.country = account.country
            language = cast("str | None", self.config.get_value(CONF_LANGUAGE))
            if language:
                self._client._locale = f"{language}_{account.country}"

            profiles = await self._client.get_profiles()
            profile = next((p for p in profiles.profiles if p.login_key == login_key), None)
            self._profile_id = profile.id if profile else profiles.profiles[0].id
            self._profile_name = profile.name if profile else None
            self.logger.info(
                "Logged in as %s (country: %s, language: %s)",
                self._profile_name,
                account.country,
                language,
            )

            self._ongoing_product_ids = set()
            self._ongoing_list_id = None
            libraries = await self._client.get_libraries()
            ongoing = next(
                (lst for lst in libraries.lists if lst.type == LibraryListType.ONGOING), None
            )
            if ongoing:
                self._ongoing_list_id = ongoing.id
                # Populate ongoing product IDs to avoid duplicate add_to_list calls
                page = 0
                while True:
                    result = await self._client.get_library(
                        LibraryListType.ONGOING, ongoing.id, page=page
                    )
                    if not result.products:
                        break
                    self._ongoing_product_ids.update(p.id for p in result.products)
                    if len(result.products) < 50:
                        break
                    page += 1
                self.logger.info("Loaded %d ongoing books", len(self._ongoing_product_ids))
        except Exception:
            await self._client.close()
            raise

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return supported features."""
        return SUPPORTED_FEATURES

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    @property
    def instance_name_postfix(self) -> str | None:
        """Return instance name postfix for this provider instance."""
        return self._profile_name

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider."""
        await self._client.close()

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Search Nextory for audiobooks."""
        if MediaType.AUDIOBOOK not in media_types:
            return SearchResults()
        results = await self._client.search_books(search_query, per=limit, sort="relevance")
        audiobooks = []
        for product in results.products:
            hls_format = self._get_hls_format(product)
            if hls_format:
                audiobooks.append(self._parse_audiobook(product, hls_format.identifier))
        return SearchResults(audiobooks=audiobooks)

    async def recommendations(self) -> list[RecommendationFolder]:
        """Get personalized recommendations from Nextory home entries."""
        folders: list[RecommendationFolder] = []

        # Add "Continue Listening" from ongoing list
        try:
            ongoing = await self._browse_list("ongoing", max_items=10)
            if ongoing:
                folders.append(
                    RecommendationFolder(
                        item_id="ongoing",
                        name="Continue Listening",
                        provider=self.instance_id,
                        items=UniqueList(ongoing),
                    )
                )
        except Exception:
            self.logger.debug("Failed to fetch ongoing list")

        # Add personalized home entries
        try:
            entries = await self._client.get_home_entries(page=0, per=5)
        except Exception:
            self.logger.debug("Failed to fetch home entries")
            return folders
        usable_types = {"selection", "top_picks", "popular"}
        for entry in entries.entries:
            if entry.type not in usable_types or not entry.selection:
                continue
            try:
                products = await self._client.get_home_entry_products(
                    entry.id,
                    per=20,
                )
            except Exception:  # noqa: S112
                continue
            items: list[Audiobook] = []
            for product in products.products:
                hls = self._get_hls_format(product)
                if hls:
                    items.append(self._parse_audiobook(product, hls.identifier))
            if items:
                folders.append(
                    RecommendationFolder(
                        item_id=f"home_{entry.id}",
                        name=entry.selection.title,
                        provider=self.instance_id,
                        items=UniqueList(items),
                    )
                )
        return folders

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items.

        Structure:
          root/
            ongoing/          → books currently reading
            want_to_read/     → saved for later
            completed/        → finished books
            categories/       → genre categories
              {category_id}/  → books in category
            series_{id}/      → books in a series
        """
        item_path = path.split("://", 1)[1] if "://" in path else path
        if not item_path:
            return self._browse_root()

        try:
            return await self._browse_path(item_path)
        except Exception:
            self.logger.debug("Browse failed for %s", item_path)
            return []

    async def _browse_path(
        self,
        item_path: str,
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Resolve a browse path to items."""
        parts = item_path.split("/")

        # Library list: ongoing, want_to_read, completed
        if parts[0] in ("ongoing", "want_to_read", "completed"):
            return await self._browse_list(parts[0])

        # Categories root
        if parts[0] == "categories" and len(parts) == 1:
            return await self._browse_categories()

        # Category detail
        if parts[0] == "categories" and len(parts) == 2:
            return await self._browse_category_products(int(parts[1]))

        # Series detail (from recommendations or categories)
        if parts[-1].startswith("series_"):
            series_id = int(parts[-1].removeprefix("series_"))
            return await self._browse_series(series_id)

        return []

    def _browse_root(self) -> list[BrowseFolder]:
        """Return top-level browse folders."""
        folders = []
        for list_type, name in [
            ("ongoing", "Currently Reading"),
            ("want_to_read", "Saved for Later"),
            ("completed", "Finished"),
            ("categories", "Categories"),
        ]:
            folders.append(
                BrowseFolder(
                    item_id=list_type,
                    provider=self.instance_id,
                    name=name,
                    path=f"{self.instance_id}://{list_type}",
                )
            )
        return folders

    async def _browse_list(self, list_type: str, max_items: int = 200) -> list[Audiobook]:
        """Browse a library list by type."""
        libraries = await self._client.get_libraries()
        lst = next((x for x in libraries.lists if x.type == list_type), None)
        if not lst:
            return []
        results: list[Audiobook] = []
        page = 0
        per = min(max_items, 50)
        while True:
            result = await self._client.get_library(list_type, lst.id, page=page, per=per)
            if not result.products:
                break
            for product in result.products:
                hls_format = self._get_hls_format(product)
                if hls_format:
                    results.append(self._parse_audiobook(product, hls_format.identifier))
            if len(results) >= max_items or len(result.products) < per:
                break
            page += 1
        return results[:max_items]

    async def _browse_categories(self) -> list[BrowseFolder]:
        """Browse content categories (cached)."""
        if self._categories_cache is not None and (
            time.monotonic() - self._categories_cache_time < 86400
        ):
            return self._categories_cache
        cats = await self._client.get_categories(content_type="book")
        self._categories_cache = [
            BrowseFolder(
                item_id=f"cat_{cat.id}",
                provider=self.instance_id,
                name=cat.title,
                path=f"{self.instance_id}://categories/{cat.id}",
                image=MediaItemImage(
                    type=ImageType.THUMB,
                    path=cat.small_image,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
                if cat.small_image
                else None,
            )
            for cat in cats.categories
        ]
        self._categories_cache_time = time.monotonic()
        return self._categories_cache

    async def _browse_category_products(self, category_id: int) -> list[Audiobook | BrowseFolder]:
        """Browse a category — show subcategories if available, otherwise books."""
        # Check if this category has subcategories (from cache)
        if self._categories_cache:
            cat = next(
                (c for c in self._categories_cache if c.item_id == f"cat_{category_id}"),
                None,
            )
            if cat:
                # Fetch full category details for subcategories
                cats = await self._client.get_categories(content_type="book")
                full_cat = next((c for c in cats.categories if c.id == category_id), None)
                if full_cat and full_cat.sub_categories:
                    return [
                        BrowseFolder(
                            item_id=f"cat_{sub.id}",
                            provider=self.instance_id,
                            name=(
                                f"{sub.title} ({sub.products_count})"
                                if sub.products_count
                                else sub.title
                            ),
                            path=f"{self.instance_id}://categories/{sub.id}",
                            image=MediaItemImage(
                                type=ImageType.THUMB,
                                path=sub.small_image,
                                provider=self.instance_id,
                                remotely_accessible=True,
                            )
                            if sub.small_image
                            else None,
                        )
                        for sub in full_cat.sub_categories
                    ]

        # No subcategories — list books
        products = await self._client.get_products_by_path(
            "categories", category_id, per=50, content_type="book"
        )
        results: list[Audiobook | BrowseFolder] = []
        for product in products.products:
            hls_format = self._get_hls_format(product)
            if hls_format:
                results.append(self._parse_audiobook(product, hls_format.identifier))
        return results

    async def _browse_series(self, series_id: int) -> list[Audiobook]:
        """Browse audiobooks in a series."""
        products = await self._client.get_products_by_path("series", series_id, per=50)
        results: list[Audiobook] = []
        for product in products.products:
            hls_format = self._get_hls_format(product)
            if hls_format:
                results.append(self._parse_audiobook(product, hls_format.identifier))
        return results

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook, None]:
        """Get audiobooks from the ongoing library."""
        libraries = await self._client.get_libraries()
        ongoing_list = next(
            (lst for lst in libraries.lists if lst.type == LibraryListType.ONGOING), None
        )
        if not ongoing_list:
            return

        page = 0
        while True:
            result = await self._client.get_library(
                LibraryListType.ONGOING, ongoing_list.id, page=page
            )
            if not result.products:
                break
            for product in result.products:
                hls_format = self._get_hls_format(product)
                if hls_format:
                    yield self._parse_audiobook(product, hls_format.identifier)
            if len(result.products) < 50:
                break
            page += 1

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details."""
        try:
            book_id, format_id = prov_audiobook_id.split("_")
            t0 = time.monotonic()
            product = await self._client.get_product_details(int(book_id))
            t1 = time.monotonic()
            audio_package = await self._client.get_audio_package(int(format_id))
            t2 = time.monotonic()
            self.logger.debug(
                "get_audiobook %s: product=%.1fs, audio_package=%.1fs",
                prov_audiobook_id,
                t1 - t0,
                t2 - t1,
            )
            audiobook = self._parse_audiobook(product, int(format_id))
            audiobook.metadata.chapters = [
                MediaItemChapter(
                    position=idx + 1,
                    name=f.title or f"Chapter {idx + 1}",
                    start=f.start_at / 1000,
                    end=f.end_at / 1000,
                )
                for idx, f in enumerate(audio_package.files)
            ]
            return audiobook
        except Exception as err:
            self._handle_nextory_error(err)

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """Get resume position for an audiobook.

        :returns: Tuple of (fully_played, elapsed_time_ms).
        """
        _, format_id = item_id.split("_")
        pos = await self._client.get_position(int(format_id))
        if pos.elapsed_time is None:
            return False, 0
        fully_played = pos.percentage is not None and pos.percentage >= 0.99
        return fully_played, pos.elapsed_time

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for playback."""
        _, format_id = item_id.split("_")
        audio_package = await self._client.get_audio_package(int(format_id))

        if not audio_package.files:
            raise MediaNotFoundError(f"No audio files for {item_id}")

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.PCM_S16LE),
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.CUSTOM,
            duration=audio_package.duration // 1000,
            can_seek=True,
            allow_seek=True,
            data={"format_id": int(format_id)},
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream by downloading and decrypting HLS segments.

        ffmpeg's crypto+https handler has a bug at METHOD=NONE → AES-128
        transitions, so we download/decrypt segments in Python and pipe
        raw AAC to ffmpeg for transcoding only.
        """
        format_id = streamdetails.data["format_id"]
        audio_package = await self._client.get_audio_package(format_id)
        self.logger.debug(
            "Streaming format %d (%d files, seek=%ds)",
            format_id,
            len(audio_package.files),
            seek_position,
        )
        seek_ms = seek_position * 1000
        http = self.mass.http_session

        for idx, chapter_file in enumerate(audio_package.files):
            chapter_end = chapter_file.start_at + chapter_file.duration
            if seek_ms >= chapter_end:
                continue

            chapter_offset = max(0, (seek_ms - chapter_file.start_at) / 1000)

            # Parse media playlist and fetch encryption key
            playlist, key_bytes = await self._fetch_chapter_playlist(chapter_file.uri)
            segments, seg_start, skip_secs = self._parse_playlist(playlist, chapter_offset)
            if chapter_offset > 0:
                seek_ms = 0

            args = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-nostats",
            ]
            if skip_secs > 0:
                args += ["-ss", str(skip_secs)]
            args += [
                "-f",
                "aac",
                "-i",
                "pipe:0",
                "-ac",
                "2",
                "-ar",
                "44100",
                "-acodec",
                "pcm_s16le",
                "-f",
                "s16le",
                "-",
            ]

            self.logger.debug(
                "Chapter %d/%d '%s': %d segments, offset=%.1fs (skip %d segs + %.1fs)",
                idx + 1,
                len(audio_package.files),
                chapter_file.title,
                len(segments),
                chapter_offset,
                seg_start,
                skip_secs,
            )

            async def _feed_stdin(
                proc: AsyncProcess,
                _seg_start: int = seg_start,
                _chapter_uri: str = chapter_file.uri,
            ) -> None:
                """Download, decrypt, and feed segments to ffmpeg stdin."""
                nonlocal segments, key_bytes
                try:
                    for seg_idx, (seg_url, iv) in enumerate(segments[_seg_start:], _seg_start):
                        async with http.get(seg_url, timeout=STREAM_TIMEOUT) as resp:
                            if resp.status == 403:
                                self.logger.info("Segment URL expired, refreshing playlist")
                                playlist_new, key_bytes = await self._fetch_chapter_playlist(
                                    _chapter_uri
                                )
                                segments = self._parse_playlist(playlist_new)[0]
                                async with http.get(
                                    segments[seg_idx][0],
                                    timeout=STREAM_TIMEOUT,
                                ) as resp2:
                                    data = await resp2.read()
                            else:
                                data = await resp.read()
                        if iv is not None and key_bytes:
                            dec = Cipher(algorithms.AES(key_bytes), modes.CBC(iv)).decryptor()
                            decrypted = dec.update(data) + dec.finalize()
                            unpadder = PKCS7(128).unpadder()
                            data = unpadder.update(decrypted) + unpadder.finalize()
                        await proc.write(data)
                except Exception:
                    self.logger.exception("Error feeding segments")
                finally:
                    await proc.write_eof()

            bytes_received = 0
            async with AsyncProcess(args, stdin=True, stdout=True, stderr=False) as proc:
                feed_task = asyncio.create_task(_feed_stdin(proc))
                proc.attach_stderr_reader(feed_task)
                async for chunk in proc.iter_any():
                    bytes_received += len(chunk)
                    yield chunk

            self.logger.debug(
                "Chapter %d/%d '%s' finished: %.1fs of expected %.1fs",
                idx + 1,
                len(audio_package.files),
                chapter_file.title,
                bytes_received / 176400,
                (chapter_file.duration / 1000) - chapter_offset,
            )

    async def _fetch_chapter_playlist(self, master_url: str) -> tuple[str, bytes | None]:
        """Fetch media playlist and encryption key for a chapter.

        Retries once on auth failure by refreshing the profile token.

        :returns: Tuple of (playlist_text, key_bytes_or_None).
        """
        http = self.mass.http_session
        media_url = master_url.replace("/master_playlist", "/media_playlist.m3u8")

        for attempt in range(2):
            headers = self._client.auth_headers
            async with http.get(media_url, headers=headers) as resp:
                playlist = await resp.text()
            if '"error"' in playlist[:50] and '"code":2002' in playlist:
                if attempt == 0 and self._client.login_key:
                    self.logger.info("Profile token expired during streaming, refreshing")
                    await self._client._refresh_profile_token()
                    continue
                raise MediaNotFoundError("Auth failed fetching playlist")
            break

        key_bytes = None
        key_match = re.search(r'URI="(https://[^"]+encryption_keys/[^"]+)"', playlist)
        if key_match:
            async with http.get(key_match.group(1), headers=self._client.auth_headers) as resp:
                key_bytes = await resp.read()
        return playlist, key_bytes

    @staticmethod
    def _parse_playlist(
        playlist: str, offset: float = 0
    ) -> tuple[list[tuple[str, bytes | None]], int, float]:
        """Parse HLS playlist into segments and calculate seek position.

        :param playlist: HLS playlist text.
        :param offset: Seek offset in seconds within the chapter.
        :returns: Tuple of (segments, segment_index, sub_segment_seconds).
        """
        segments: list[tuple[str, bytes | None]] = []
        current_iv: bytes | None = None
        encrypted = False
        cumulative = 0.0
        dur = 0.0
        seg_start = 0
        skip_secs = 0.0
        seek_found = offset <= 0

        for line in playlist.split("\n"):
            if line.startswith("#EXT-X-KEY"):
                if "METHOD=NONE" in line:
                    encrypted = False
                elif "METHOD=AES-128" in line:
                    encrypted = True
                    iv_m = re.search(r"IV=0x([0-9a-fA-F]+)", line)
                    if iv_m:
                        current_iv = bytes.fromhex(iv_m.group(1))
            elif line.startswith("#EXTINF:"):
                dur = float(line.split(":")[1].split(",")[0])
            elif line.startswith("http"):
                if not seek_found:
                    if cumulative + dur > offset:
                        seg_start = len(segments)
                        skip_secs = offset - cumulative
                        seek_found = True
                    cumulative += dur
                segments.append((line.strip(), current_iv if encrypted else None))

        if not seek_found:
            seg_start = len(segments)
        return segments, seg_start, skip_secs

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Report playback position back to Nextory."""
        try:
            book_id_str, format_id = prov_item_id.split("_")
            book_id = int(book_id_str)
            fmt_id = int(format_id)
            self.logger.debug(
                "on_played: book=%s format=%s pos=%d fully_played=%s",
                book_id_str,
                format_id,
                position or 0,
                fully_played,
            )
            duration = (
                media_item.duration
                if hasattr(media_item, "duration") and media_item.duration
                else 1
            )
            position = position or 0

            # Mark unplayed: position=0 and not fully_played
            if position == 0 and not fully_played:
                if self._ongoing_list_id and book_id in self._ongoing_product_ids:
                    await self._client.remove_from_library(book_id, self._ongoing_list_id)
                    self._ongoing_product_ids.discard(book_id)
                return

            percentage = 1.0 if fully_played else round(min(1.0, position / duration), 4)
            elapsed_ms = position * 1000

            if self._ongoing_list_id and book_id not in self._ongoing_product_ids:
                await self._client.add_to_list(book_id, self._ongoing_list_id)
                self._ongoing_product_ids.add(book_id)

            if fully_played and self._ongoing_list_id and book_id in self._ongoing_product_ids:
                await self._client.remove_from_library(book_id, self._ongoing_list_id)
                self._ongoing_product_ids.discard(book_id)
                await self._client.mark_completed(book_id)

            await self._client.patch_position(
                profile_id=self._profile_id,
                format_id=fmt_id,
                percentage=percentage,
                elapsed_time=elapsed_ms,
            )
        except Exception:
            self.logger.exception("Failed to report playback position")

    @staticmethod
    def _handle_nextory_error(err: Exception) -> NoReturn:
        """Re-raise Nextory errors as MA-friendly exceptions."""
        if isinstance(err, MaxProfileSessionsError):
            raise ProviderUnavailableError(err.description or str(err)) from err
        if isinstance(err, (ExpiredLoginTokenError, InvalidAuthTokenError)):
            raise LoginFailed(err.description or str(err)) from err
        if isinstance(err, NextoryApiError):
            raise ProviderUnavailableError(err.description or str(err)) from err
        if isinstance(err, TimeoutError):
            raise ProviderUnavailableError("Nextory not responding.") from err
        raise ProviderUnavailableError(f"Nextory error: {err}") from err

    @staticmethod
    def _get_hls_format(product: ProductResponse) -> FormatResponse | None:
        """Get the active HLS format from a product, if available."""
        return next(
            (
                f
                for f in product.formats
                if f.type == FormatType.HLS and f.state == FormatState.ACTIVE
            ),
            None,
        )

    def _parse_audiobook(self, product: ProductResponse, format_id: int) -> Audiobook:
        """Parse Nextory product to Music Assistant Audiobook."""
        item_id = f"{product.id}_{format_id}"
        hls_format = next((f for f in product.formats if f.identifier == format_id), None)

        audiobook = Audiobook(
            item_id=item_id,
            provider=self.instance_id,
            name=product.title,
            duration=hls_format.duration if hls_format and hls_format.duration else 0,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

        audiobook.authors.set([a.name for a in product.authors])
        audiobook.narrators.set([n.name for n in product.narrators])
        audiobook.metadata.description = product.description_full or product.blurb

        if hls_format and hls_format.img_url:
            audiobook.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=hls_format.img_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )

        return audiobook
