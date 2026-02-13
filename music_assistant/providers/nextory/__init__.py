"""Nextory provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from nextory import NextoryClient
from nextory.models import FormatType, LibraryListType, ProductResponse

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_LOGIN_TOKEN = "login_token"
CONF_LOGIN_KEY = "login_key"
CONF_PROFILE_TOKEN = "profile_token"
CONF_ACTION_AUTH = "authenticate"
CONF_ACTION_SELECT_PROFILE = "select_profile"

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.SEARCH,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider with given configuration."""
    return NextoryProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    Multi-step auth flow: Authenticate → Select Profile → Save.
    """
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

        async with NextoryClient(auto_select_profile=True) as client:
            login_token = await client.login(username, password)
            values[CONF_LOGIN_TOKEN] = login_token

    # Build profile options if authenticated
    profile_options: list[ConfigValueOption] = []
    if login_token and not profile_token:
        async with NextoryClient(login_token=login_token, auto_select_profile=True) as client:
            profiles_resp = await client.get_profiles()
            for profile in profiles_resp.profiles:
                profile_options.append(ConfigValueOption(profile.name, profile.login_key))

    # Handle profile selection action
    login_key = cast("str | None", values.get(CONF_LOGIN_KEY))
    if action == CONF_ACTION_SELECT_PROFILE and login_token and login_key and not profile_token:
        async with NextoryClient(login_token=login_token, auto_select_profile=True) as client:
            profile_token = await client.select_profile(login_key)
            values[CONF_PROFILE_TOKEN] = profile_token

    is_authenticated = bool(profile_token)
    needs_profile = bool(login_token and not profile_token)

    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=not is_authenticated,
            hidden=is_authenticated,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=not is_authenticated,
            hidden=is_authenticated,
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
            hidden=is_authenticated,
            options=profile_options if profile_options else [],
            value=login_key,
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
    _ongoing_list_id: str | None
    _ongoing_product_ids: set[int]

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
            auto_select_profile=True,
            profile_token=profile_token,
        )

        # Resolve profile_id for position sync
        profiles = await self._client.get_profiles()
        profile = next((p for p in profiles.profiles if p.login_key == login_key), None)
        self._profile_id = profile.id if profile else profiles.profiles[0].id

        # Cache ongoing list ID for library management
        self._ongoing_product_ids = set()
        self._ongoing_list_id = None
        libraries = await self._client.get_libraries()
        ongoing = next(
            (lst for lst in libraries.lists if lst.type == LibraryListType.ONGING), None
        )
        if ongoing:
            self._ongoing_list_id = ongoing.id

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return supported features."""
        return SUPPORTED_FEATURES

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
            hls_format = next((f for f in product.formats if f.type == FormatType.HLS), None)
            if hls_format:
                audiobooks.append(self._parse_audiobook(product, hls_format.identifier))
        return SearchResults(audiobooks=audiobooks)

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook, None]:
        """Get audiobooks from the ongoing library."""
        libraries = await self._client.get_libraries()
        ongoing_list = next(
            (lst for lst in libraries.lists if lst.type == LibraryListType.ONGING), None
        )
        if not ongoing_list:
            return

        products = await self._client.get_library(LibraryListType.ONGING, ongoing_list.id)
        for product in products.products:
            hls_format = next((f for f in product.formats if f.type == FormatType.HLS), None)
            if hls_format:
                yield self._parse_audiobook(product, hls_format.identifier)

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details."""
        book_id, format_id = prov_audiobook_id.split("_")
        product = await self._client.get_product_details(int(book_id))
        return self._parse_audiobook(product, int(format_id))

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
            audio_format=AudioFormat(content_type=ContentType.FLAC),
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
        """Return the audio stream by letting ffmpeg handle HLS natively.

        Iterates over chapters sequentially, running one ffmpeg per chapter.
        Auth headers are passed via -headers so ffmpeg can fetch playlists,
        encryption keys, and segments directly.
        """
        from music_assistant.helpers.ffmpeg import get_ffmpeg_stream

        format_id = streamdetails.data["format_id"]
        audio_package = await self._client.get_audio_package(format_id)
        seek_ms = seek_position * 1000
        headers = self._get_ffmpeg_headers()

        for chapter_file in audio_package.files:
            chapter_end = chapter_file.start_at + chapter_file.duration
            if seek_ms >= chapter_end:
                continue  # skip entire chapter

            extra_input_args = [
                "-headers", headers,
                "-allowed_extensions", "ALL",
            ]
            # Seek within this chapter
            chapter_offset = max(0, (seek_ms - chapter_file.start_at) / 1000)
            if chapter_offset > 0:
                extra_input_args += ["-ss", str(chapter_offset)]
                seek_ms = 0  # only seek in the first chapter

            async for chunk in get_ffmpeg_stream(
                audio_input=chapter_file.uri,
                input_format=AudioFormat(content_type=ContentType.UNKNOWN),
                output_format=AudioFormat(content_type=ContentType.FLAC),
                extra_input_args=extra_input_args,
            ):
                yield chunk

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
        book_id_str, format_id = prov_item_id.split("_")
        book_id = int(book_id_str)
        fmt_id = int(format_id)
        duration = media_item.duration if media_item.duration else 1
        percentage = 1.0 if fully_played else round(min(1.0, position / duration), 4)
        elapsed_ms = position * 1000

        # Add to ongoing list if not already there
        if self._ongoing_list_id and book_id not in self._ongoing_product_ids:
            url = "https://api.nextory.com/library/v1/me/custom_lists/operations"
            async with self._client.post(
                url,
                json={
                    "operations": [
                        {"product_id": book_id, "list_id": self._ongoing_list_id, "type": "add"}
                    ]
                },
            ) as resp:
                await resp.read()
            self._ongoing_product_ids.add(book_id)

        # Remove from ongoing if fully played
        if fully_played and self._ongoing_list_id and book_id in self._ongoing_product_ids:
            url = "https://api.nextory.com/library/v1/me/custom_lists/operations"
            async with self._client.post(
                url,
                json={
                    "operations": [
                        {"product_id": book_id, "list_id": self._ongoing_list_id, "type": "remove"}
                    ]
                },
            ) as resp:
                await resp.read()
            self._ongoing_product_ids.discard(book_id)
            # Mark as completed in Nextory
            async with self._client.post(
                f"https://api.nextory.com/library/v1/me/products/{book_id}/completed"
            ) as resp:
                await resp.read()

        await self._client.patch_position(
            profile_id=self._profile_id,
            format_id=fmt_id,
            percentage=percentage,
            elapsed_time=elapsed_ms,
        )

    def _get_ffmpeg_headers(self) -> str:
        """Build ffmpeg -headers string from client auth state."""
        login_mw = self._client._middlewares[0]
        profile_mw = self._client._middlewares[1]
        headers = {**login_mw._headers}
        if login_mw.login_token:
            headers["X-Login-Token"] = login_mw.login_token
        if login_mw.country:
            headers["X-Country-Code"] = login_mw.country
        if profile_mw.profile_token:
            headers["X-Profile-Token"] = profile_mw.profile_token
        return "".join(f"{k}: {v}\r\n" for k, v in headers.items())

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
                    )
                ]
            )

        return audiobook
