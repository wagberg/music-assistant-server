"""Nextory provider for Music Assistant."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, NoReturn, cast

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from music_assistant_models.enums import (
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
    ExpiredProfileTokenError,
    InvalidAuthTokenError,
    MaxProfileSessionsError,
    NextoryApiError,
)
from nextory.models import FormatResponse, FormatState, FormatType, LibraryListType, ProductResponse

from music_assistant.helpers.process import AsyncProcess
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin
from music_assistant.providers.nextory.constants import (
    CONF_LANGUAGE,
    CONF_LOGIN_KEY,
    CONF_LOGIN_TOKEN,
    CONF_PROFILE_TOKEN,
)

STREAM_TIMEOUT = aiohttp.ClientTimeout(total=30)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

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


class NextoryProvider(RecommendationPayloadMixin, MusicProvider):
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
        login_token = cast("str", self.get_setup_value(CONF_LOGIN_TOKEN))
        profile_token = cast("str", self.get_setup_value(CONF_PROFILE_TOKEN))
        login_key = cast("str", self.get_setup_value(CONF_LOGIN_KEY))

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
            language = cast("str | None", self.get_setup_value(CONF_LANGUAGE))
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
        await super().unload(is_removed)
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

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Return personalized recommendation folders without items."""
        return await self._recommendation_rows_from_payload()

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return the items for a single recommendation folder.

        :param item_id: the item id of the recommendation row.
        """
        return await self._recommendation_items_from_payload(item_id)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

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

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
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

    async def get_resume_position(
        self,
        item_id: str,
        media_type: MediaType,
    ) -> tuple[bool, int, datetime | None]:
        """
        Get resume position for an audiobook.

        :returns: Tuple of (fully_played, elapsed_time_ms, reached_at).
        """
        _, format_id = item_id.split("_")
        pos = await self._client.get_position(int(format_id))
        if pos.elapsed_time is None:
            return False, 0, None
        fully_played = pos.percentage is not None and pos.percentage >= 0.99
        return fully_played, pos.elapsed_time, pos.reached_at

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
    ) -> AsyncGenerator[bytes]:
        """
        Return the audio stream by downloading and decrypting HLS segments.

        Nextory leaves every 6th segment unencrypted, so a chapter playlist toggles
        between METHOD=NONE and AES-128 ~62 times. ffmpeg's native HLS demuxer reuses
        its keep-alive HTTP connection across those transitions and corrupts the first
        one or two encrypted segments after each one, silently discarding 17-37% of the
        audio. We therefore download and decrypt segments here and pipe raw AAC to
        ffmpeg for transcoding only. See STREAMING.md for the full diagnosis; the
        ffmpeg-native alternative requires -http_persistent 0.
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
                chapter_file.title or chapter_file.idref,
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
                chapter_file.title or chapter_file.idref,
                bytes_received / 176400,
                (chapter_file.duration / 1000) - chapter_offset,
            )

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

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        """Fetch personalized recommendations from Nextory home entries, with items."""
        folders: list[RecommendationFolder] = []

        # Add "Continue Listening" from ongoing list
        try:
            ongoing = await self._browse_list("ongoing", max_items=10)
            if ongoing:
                folders.append(
                    RecommendationFolder(
                        item_id="ongoing",
                        name="Continue Listening",
                        translation_key="in_progress_items",
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

    async def _fetch_chapter_playlist(self, master_url: str) -> tuple[str, bytes | None]:
        """
        Fetch media playlist and encryption key for a chapter.

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
        """
        Parse HLS playlist into segments and calculate seek position.

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

    @staticmethod
    def _handle_nextory_error(err: Exception) -> NoReturn:
        """Re-raise Nextory errors as MA-friendly exceptions."""
        if isinstance(err, MaxProfileSessionsError):
            # The account's concurrent-stream tier is already in use by other profiles.
            raise ProviderUnavailableError(
                f"Nextory concurrent stream limit reached: {err.description or err}"
            ) from err
        if isinstance(err, ExpiredProfileTokenError):
            # The client already retries once on this (client.py's own 401 handling);
            # a second failure means the profile was taken again immediately, e.g. by
            # another profile logging in right after our refresh.
            raise ProviderUnavailableError(
                f"Nextory profile session was taken by another device: {err.description or err}"
            ) from err
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
