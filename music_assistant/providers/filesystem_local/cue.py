"""CUE sheet integration for the filesystem_local provider.

A CUE sheet describes multiple logical tracks within a single audio file
(typically a whole-album rip). This module provides:

- Synthetic ``item_id`` construction/parsing for CUE-derived tracks.
- A :class:`CueSheetHandler` that turns a CUE sheet + its audio file into
  :class:`Track` objects, produces :class:`StreamDetails` for playback of a
  single logical track, and streams the requested segment via FFmpeg.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiofiles
from music_assistant_models.enums import (
    ContentType,
    ExternalID,
    ImageType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.cue_sheet import CueSheet, CueTrack, parse_cue_sheet
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.tags import AudioTags, async_parse_tags

from .constants import TRACK_EXTENSIONS
from .helpers import FileSystemItem, get_relative_path

if TYPE_CHECKING:
    from . import LocalFileSystemProvider

CUE_TRACK_ID_DELIMITER = "::track"


@dataclass(frozen=True, slots=True)
class _TrackBuildContext:
    """Shared state used to build each Track from a CUE sheet."""

    audio_format: AudioFormat
    disc_number: int
    date_added: datetime | None
    embedded_image: MediaItemImage | None
    track_genres: set[str] | None
    album: Album | None
    album_performer: str | None


def make_cue_track_id(cue_relative_path: str, track_number: int) -> str:
    """Build the synthetic provider item_id for a CUE-derived track."""
    return f"{cue_relative_path}{CUE_TRACK_ID_DELIMITER}{track_number:02d}"


def parse_cue_track_id(item_id: str) -> tuple[str, int] | None:
    """Return (cue_relative_path, track_number) if item_id is a CUE track id, else None."""
    if CUE_TRACK_ID_DELIMITER not in item_id:
        return None
    cue_path, track_num_str = item_id.rsplit(CUE_TRACK_ID_DELIMITER, 1)
    return cue_path, int(track_num_str)


class CueSheetHandler:
    """CUE sheet integration bound to a :class:`LocalFileSystemProvider` instance."""

    def __init__(self, provider: LocalFileSystemProvider) -> None:
        """
        Initialize the handler.

        :param provider: The provider that owns this handler; used for shared state
            (cache, logger, mass) and helpers (``_parse_album``, ``_parse_artist``).
        """
        self.provider = provider

    async def read_cue_file(self, absolute_path: str) -> str:
        """
        Read CUE file content.

        :param absolute_path: Absolute path to the CUE file.
        """
        for encoding in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                async with aiofiles.open(absolute_path, encoding=encoding) as f:
                    content: str = await f.read()
                    return content
            except UnicodeDecodeError:
                continue
        msg = f"Unable to decode CUE file: {absolute_path}"
        raise InvalidDataError(msg)

    async def load_cue_sheet(self, cue_item: FileSystemItem) -> CueSheet:
        """
        Read and parse a CUE sheet file.

        :param cue_item: The CUE file's FileSystemItem.
        """
        content = await self.read_cue_file(cue_item.absolute_path)
        return parse_cue_sheet(content)

    @staticmethod
    def _audio_format_from_tags(audio_path: str, tags: AudioTags) -> AudioFormat:
        """
        Build an AudioFormat for an audio file from its tags.

        :param audio_path: Path to the audio file (used for extension fallback).
        :param tags: Parsed audio tags.
        """
        ext = audio_path.rsplit(".", 1)[-1] if "." in audio_path else None
        return AudioFormat(
            content_type=ContentType.try_parse(ext or tags.format),
            sample_rate=tags.sample_rate,
            bit_depth=tags.bits_per_sample,
            channels=tags.channels,
            bit_rate=tags.bit_rate,
        )

    async def find_audio_file(self, cue_item: FileSystemItem, cue_sheet: CueSheet) -> str | None:
        """
        Find the audio file referenced by a CUE sheet.

        :param cue_item: The CUE file's FileSystemItem.
        :param cue_sheet: The parsed CUE sheet data.
        """

        def _locate() -> str | None:
            cue_dir = os.path.dirname(cue_item.absolute_path)

            # 1. try the filename from the CUE FILE command
            if cue_sheet.file_path:
                candidate = os.path.join(cue_dir, cue_sheet.file_path)
                if os.path.isfile(candidate):
                    return candidate

            # 2. same-name matching: album.cue -> album.{flac,mp3,...}
            cue_stem = cue_item.filename.rsplit(".", 1)[0]
            for ext in TRACK_EXTENSIONS:
                candidate = os.path.join(cue_dir, f"{cue_stem}.{ext}")
                if os.path.isfile(candidate):
                    return candidate

            # 3. fall back to the only audio file in the same directory, if any
            audio_files: list[str] = []
            try:
                for entry in os.scandir(cue_dir):
                    if not entry.is_file() or "." not in entry.name:
                        continue
                    if entry.name.rsplit(".", 1)[1].lower() in TRACK_EXTENSIONS:
                        audio_files.append(entry.path)
            except OSError as err:
                self.provider.logger.warning("Unable to scan CUE directory %s: %s", cue_dir, err)
                return None
            if len(audio_files) == 1:
                return audio_files[0]
            return None

        return await asyncio.to_thread(_locate)

    @staticmethod
    def _apply_cue_overrides(tags: AudioTags, cue_sheet: CueSheet) -> None:
        """Overwrite album-level audio tags with values from the CUE sheet."""
        if cue_sheet.title:
            tags.tags["album"] = cue_sheet.title
        if cue_sheet.performer:
            tags.tags.pop("albumartists", None)
            tags.tags["albumartist"] = cue_sheet.performer
        if cue_sheet.date:
            tags.tags["date"] = cue_sheet.date
        if cue_sheet.genre:
            tags.tags["genre"] = cue_sheet.genre
        if cue_sheet.musicbrainz_albumid:
            tags.tags["musicbrainzalbumid"] = cue_sheet.musicbrainz_albumid

    async def _build_track(
        self,
        cue_track: CueTrack,
        cue_item: FileSystemItem,
        duration: float,
        ctx: _TrackBuildContext,
    ) -> Track:
        """Construct a single Track from a CUE track entry."""
        provider = self.provider
        track_id = make_cue_track_id(cue_item.relative_path, cue_track.number)

        # track artist: CUE track PERFORMER → CUE top-level PERFORMER → none
        track_artists: UniqueList[Artist | ItemMapping] = UniqueList()
        if track_performer := cue_track.performer or ctx.album_performer:
            if artist := await provider._parse_artist(name=track_performer):
                track_artists.append(artist)

        track = Track(
            item_id=track_id,
            provider=provider.instance_id,
            name=cue_track.title or f"Track {cue_track.number}",
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=provider.domain,
                    provider_instance=provider.instance_id,
                    audio_format=ctx.audio_format,
                    details=cue_item.checksum,
                    in_library=True,
                )
            },
            track_number=cue_track.number,
            disc_number=ctx.disc_number,
            duration=round(duration),
            date_added=ctx.date_added,
        )

        if track_artists:
            track.artists = track_artists
        if ctx.album:
            track.album = ctx.album
        if cue_track.isrc:
            track.external_ids.add((ExternalID.ISRC, cue_track.isrc))
        if cue_track.musicbrainz_trackid:
            track.external_ids.add((ExternalID.MB_RECORDING, cue_track.musicbrainz_trackid))
        if ctx.embedded_image is not None:
            track.metadata.images = UniqueList([ctx.embedded_image])
        if ctx.track_genres is not None:
            track.metadata.genres = ctx.track_genres
        return track

    async def parse_tracks(self, cue_item: FileSystemItem) -> list[Track]:
        """
        Parse a CUE sheet and return individual :class:`Track` objects.

        :param cue_item: The CUE file's FileSystemItem.
        """
        provider = self.provider
        logger = provider.logger
        cue_sheet = await self.load_cue_sheet(cue_item)

        if not cue_sheet.tracks:
            logger.warning("CUE sheet has no tracks: %s", cue_item.relative_path)
            return []

        audio_path = await self.find_audio_file(cue_item, cue_sheet)
        if audio_path is None:
            logger.error("Audio file not found for CUE sheet: %s", cue_item.relative_path)
            return []

        tags = await async_parse_tags(audio_path)
        total_duration = tags.duration or 0.0
        if total_duration <= 0:
            logger.error(
                "Could not determine duration for audio file of CUE sheet: %s",
                cue_item.relative_path,
            )
            return []

        audio_relative_path = get_relative_path(provider.base_path, audio_path)
        self._apply_cue_overrides(tags, cue_sheet)

        album: Album | None = None
        if tags.album:
            album = await provider._parse_album(
                track_path=audio_relative_path,
                track_tags=tags,
                track_created_at=cue_item.created_at,
            )
        else:
            logger.warning(
                "CUE sheet %s has no TITLE and audio file has no album tag",
                cue_item.relative_path,
            )

        # embedded cover art is shared across all CUE tracks from this audio file
        embedded_image = (
            MediaItemImage(
                type=ImageType.THUMB,
                path=audio_relative_path,
                provider=provider.instance_id,
                remotely_accessible=False,
            )
            if tags.has_cover_image
            else None
        )
        # if the album lacks its own image, adopt the embedded one
        if album and embedded_image and not album.image:
            album.metadata.images = UniqueList([embedded_image])

        ctx = _TrackBuildContext(
            audio_format=self._audio_format_from_tags(audio_path, tags),
            # honor audio file's DISCNUMBER (CUE does not carry disc info); defaults to 1
            disc_number=tags.disc or 1,
            date_added=(
                datetime.fromtimestamp(cue_item.created_at, tz=UTC) if cue_item.created_at else None
            ),
            embedded_image=embedded_image,
            track_genres=set(tags.genres) if tags.genres else None,
            album=album,
            album_performer=cue_sheet.performer,
        )

        sorted_tracks = sorted(cue_sheet.tracks, key=lambda t: t.start_position)
        tracks: list[Track] = []
        for i, cue_track in enumerate(sorted_tracks):
            if i + 1 < len(sorted_tracks):
                duration = sorted_tracks[i + 1].start_position - cue_track.start_position
            else:
                duration = total_duration - cue_track.start_position

            if duration <= 0:
                logger.warning(
                    "CUE sheet %s track %d has non-positive duration (%.2fs); skipping",
                    cue_item.relative_path,
                    cue_track.number,
                    duration,
                )
                continue
            if not cue_track.title:
                logger.warning(
                    "CUE sheet %s track %d has no TITLE; skipping",
                    cue_item.relative_path,
                    cue_track.number,
                )
                continue

            tracks.append(await self._build_track(cue_track, cue_item, duration, ctx))

        return tracks

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """
        Return the streamdetails for a CUE-sheet-derived track.

        :param item_id: Track ID in format "path/to/file.cue::trackNN".
        """
        parsed = parse_cue_track_id(item_id)
        if parsed is None:
            msg = f"Invalid CUE track id: {item_id}"
            raise InvalidDataError(msg)
        cue_path, track_number = parsed

        cue_item = await self.provider.resolve(cue_path)
        cue_sheet = await self.load_cue_sheet(cue_item)

        audio_path = await self.find_audio_file(cue_item, cue_sheet)
        if audio_path is None:
            msg = f"Audio file not found for CUE sheet: {cue_path}"
            raise MediaNotFoundError(msg)

        tags = await async_parse_tags(audio_path)
        total_duration = tags.duration or 0.0

        cue_track = next((t for t in cue_sheet.tracks if t.number == track_number), None)
        if cue_track is None:
            msg = f"Track {track_number} not found in CUE sheet: {cue_path}"
            raise MediaNotFoundError(msg)

        start_seconds = cue_track.start_position
        sorted_tracks = sorted(cue_sheet.tracks, key=lambda t: t.start_position)
        track_idx = next(i for i, t in enumerate(sorted_tracks) if t.number == track_number)
        if track_idx + 1 < len(sorted_tracks):
            end_seconds = sorted_tracks[track_idx + 1].start_position
        else:
            end_seconds = total_duration
        duration = end_seconds - start_seconds

        # store original audio format info for get_audio_stream
        original_format = self._audio_format_from_tags(audio_path, tags)

        # output format: PCM since we use FFmpeg to extract the segment
        output_format = AudioFormat(
            content_type=ContentType.PCM_F32LE,
            sample_rate=tags.sample_rate,
            bit_depth=32,
            channels=tags.channels,
        )

        # StreamType.CUSTOM is required here: a CUE track is a segment of a larger
        # file and needs -ss/-t applied relative to the track's base offset. Core
        # appends its own -ss for user seeks after streamdetails.extra_input_args,
        # and a second input -ss overrides the first — so LOCAL_FILE with a base
        # offset cannot coexist with user seeking under the current core API.
        return StreamDetails(
            provider=self.provider.instance_id,
            item_id=item_id,
            audio_format=output_format,
            media_type=MediaType.TRACK,
            stream_type=StreamType.CUSTOM,
            duration=round(duration),
            can_seek=True,
            allow_seek=True,
            data={
                "audio_path": audio_path,
                "start_seconds": start_seconds,
                "track_duration": duration,
                "original_format": original_format.to_dict(),
            },
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """
        Yield the segment of the underlying audio file for a CUE-derived track.

        :param streamdetails: Streamdetails previously built by :meth:`get_stream_details`.
        :param seek_position: Position (seconds) within the track to start from.
        """
        if not streamdetails.data or "audio_path" not in streamdetails.data:
            msg = f"Invalid CUE track stream details: {streamdetails.item_id}"
            raise InvalidDataError(msg)

        audio_path: str = streamdetails.data["audio_path"]
        base_start: float = streamdetails.data["start_seconds"]
        track_duration: float = streamdetails.data["track_duration"]
        original_format = AudioFormat.from_dict(streamdetails.data["original_format"])

        # actual seek position within the full audio file
        actual_seek = base_start + seek_position
        remaining_duration = track_duration - seek_position

        if remaining_duration <= 0:
            return

        async for chunk in get_ffmpeg_stream(
            audio_input=audio_path,
            input_format=original_format,
            output_format=streamdetails.audio_format,
            extra_input_args=["-ss", str(actual_seek), "-t", str(remaining_duration)],
        ):
            yield chunk
