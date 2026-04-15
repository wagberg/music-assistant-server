"""Integration tests for CUE sheet support in the filesystem provider."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, ExternalID
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, Track
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.tags import AudioTags
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.cue import (
    CUE_TRACK_ID_DELIMITER,
    CueSheetHandler,
    make_cue_track_id,
    parse_cue_track_id,
)
from music_assistant.providers.filesystem_local.helpers import FileSystemItem

SAMPLE_CUE = """\
REM GENRE "Classic Rock"
REM DATE 1995
PERFORMER "Dire Straits"
TITLE "Live at the BBC"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Down to the Waterline"
    PERFORMER "Dire Straits"
    ISRC GBAMU7800001
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Six Blade Knife"
    PERFORMER "Dire Straits"
    ISRC GBAMU7800002
    INDEX 01 04:10:40
  TRACK 03 AUDIO
    TITLE "Water of Love"
    PERFORMER "Dire Straits"
    ISRC GBAMU7800003
    INDEX 01 07:58:05
"""


def _make_audio_tags(
    duration: float = 900.0,
    album: str | None = None,
    albumartist: str | None = None,
    genre: str | None = None,
    disc: str | None = None,
    has_cover_image: bool = False,
    **extra_tags: str,
) -> AudioTags:
    """Build a minimal AudioTags object for tests."""
    tag_dict: dict[str, str] = {}
    if album is not None:
        tag_dict["album"] = album
    if albumartist is not None:
        tag_dict["albumartist"] = albumartist
    if genre is not None:
        tag_dict["genre"] = genre
    if disc is not None:
        tag_dict["disc"] = disc
    tag_dict.update(extra_tags)
    return AudioTags(
        raw={},
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="flac",
        bit_rate=1000,
        duration=duration,
        tags=tag_dict,
        has_cover_image=has_cover_image,
        filename="album.flac",
    )


def _make_provider(base_path: str = "/music") -> LocalFileSystemProvider:
    """Build a LocalFileSystemProvider with dependencies mocked."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.media_content_type = "music"
    provider.base_path = base_path
    # instance_id and domain are read-only properties sourced from config/manifest
    provider.config = MagicMock(instance_id="filesystem_local--test")
    provider.manifest = MagicMock(domain="filesystem_local")
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.cache = MagicMock()
    provider._sync_tracks = True
    provider._cue = CueSheetHandler(provider)
    return provider


def _make_cue_item(tmp_path: Path, cue_text: str, name: str = "album.cue") -> FileSystemItem:
    """Write a CUE file under tmp_path and return a FileSystemItem for it."""
    cue_file = tmp_path / name
    cue_file.write_text(cue_text, encoding="utf-8")
    return FileSystemItem(
        filename=name,
        relative_path=name,
        absolute_path=str(cue_file),
        is_dir=False,
        checksum="1",
        file_size=cue_file.stat().st_size,
        created_at=1700000000,
    )


class TestCueTrackIdHelpers:
    """Tests for CUE track id construction and parsing."""

    def test_make_format(self) -> None:
        """Make format."""
        assert make_cue_track_id("album.cue", 3) == f"album.cue{CUE_TRACK_ID_DELIMITER}03"

    def test_make_pads_single_digits(self) -> None:
        """Make pads single digits."""
        assert make_cue_track_id("a.cue", 1).endswith("01")

    def test_make_handles_large_track_numbers(self) -> None:
        """Make handles large track numbers."""
        assert make_cue_track_id("a.cue", 123).endswith("123")

    def test_parse_roundtrip(self) -> None:
        """Parse roundtrip."""
        for track_num in (1, 9, 10, 99):
            item_id = make_cue_track_id("artist/album.cue", track_num)
            parsed = parse_cue_track_id(item_id)
            assert parsed == ("artist/album.cue", track_num)

    def test_parse_non_cue_id_returns_none(self) -> None:
        """Parse non cue id returns none."""
        assert parse_cue_track_id("regular/path/track.flac") is None
        assert parse_cue_track_id("") is None


class TestReadCueFile:
    """Tests for CUE file encoding handling."""

    @pytest.mark.asyncio
    async def test_reads_utf8(self, tmp_path: Path) -> None:
        """Reads utf8."""
        cue = tmp_path / "a.cue"
        cue.write_text('TITLE "Café"\n', encoding="utf-8")
        provider = _make_provider()
        content = await provider._cue.read_cue_file(str(cue))
        assert "Café" in content

    @pytest.mark.asyncio
    async def test_reads_utf8_bom(self, tmp_path: Path) -> None:
        """Reads utf8 bom."""
        cue = tmp_path / "a.cue"
        cue.write_text('TITLE "Café"\n', encoding="utf-8-sig")
        provider = _make_provider()
        content = await provider._cue.read_cue_file(str(cue))
        assert "Café" in content
        assert not content.startswith("\ufeff")

    @pytest.mark.asyncio
    async def test_falls_back_to_latin1(self, tmp_path: Path) -> None:
        """Falls back to latin1."""
        cue = tmp_path / "a.cue"
        # 0xFC = "ü" in Latin-1 but invalid as UTF-8 continuation, forcing fallback
        cue.write_bytes(b'TITLE "M\xfcller"\n')
        provider = _make_provider()
        content = await provider._cue.read_cue_file(str(cue))
        assert "Müller" in content


class TestFindCueAudioFile:
    """Tests for audio file resolution from a CUE sheet."""

    @pytest.mark.asyncio
    async def test_matches_file_command(self, tmp_path: Path) -> None:
        """Matches file command."""
        (tmp_path / "album.flac").write_bytes(b"")
        (tmp_path / "other.flac").write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, 'FILE "album.flac" WAVE\n')
        provider = _make_provider()
        cue_sheet = MagicMock(file_path="album.flac")
        result = await provider._cue.find_audio_file(cue_item, cue_sheet)
        assert result == str(tmp_path / "album.flac")

    @pytest.mark.asyncio
    async def test_same_stem_fallback(self, tmp_path: Path) -> None:
        """Same stem fallback."""
        (tmp_path / "album.flac").write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, "")
        provider = _make_provider()
        cue_sheet = MagicMock(file_path=None)
        result = await provider._cue.find_audio_file(cue_item, cue_sheet)
        assert result == str(tmp_path / "album.flac")

    @pytest.mark.asyncio
    async def test_single_audio_file_fallback(self, tmp_path: Path) -> None:
        """Single audio file fallback."""
        # FILE command points at a nonexistent file; CUE stem doesn't match
        (tmp_path / "onlyone.flac").write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, "", name="different.cue")
        provider = _make_provider()
        cue_sheet = MagicMock(file_path="missing.flac")
        result = await provider._cue.find_audio_file(cue_item, cue_sheet)
        assert result == str(tmp_path / "onlyone.flac")

    @pytest.mark.asyncio
    async def test_multiple_audio_files_no_match(self, tmp_path: Path) -> None:
        """Multiple audio files no match."""
        (tmp_path / "one.flac").write_bytes(b"")
        (tmp_path / "two.flac").write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, "", name="different.cue")
        provider = _make_provider()
        cue_sheet = MagicMock(file_path=None)
        result = await provider._cue.find_audio_file(cue_item, cue_sheet)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_audio_files(self, tmp_path: Path) -> None:
        """No audio files."""
        cue_item = _make_cue_item(tmp_path, "")
        provider = _make_provider()
        cue_sheet = MagicMock(file_path=None)
        result = await provider._cue.find_audio_file(cue_item, cue_sheet)
        assert result is None


class TestParseCueTracks:
    """Tests for _parse_cue_tracks end-to-end (with mocked parse_tags/parse_album)."""

    @staticmethod
    def _wire_provider_for_parse(
        provider: LocalFileSystemProvider, album: Album | None = None
    ) -> None:
        """Install common mocks for _parse_cue_tracks."""
        provider._parse_album = AsyncMock(return_value=album)  # type: ignore[method-assign]
        provider._parse_artist = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda name, **_k: Artist(
                item_id=name,
                provider=provider.instance_id,
                name=name,
                provider_mappings=set(),
            )
        )

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_tracks(self, tmp_path: Path) -> None:
        """Returns empty when no tracks."""
        cue_item = _make_cue_item(tmp_path, 'TITLE "Empty"\n')
        provider = _make_provider(base_path=str(tmp_path))
        provider._parse_album = AsyncMock(return_value=None)  # type: ignore[method-assign]

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=_make_audio_tags()),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)
        assert tracks == []
        provider.logger.warning.assert_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_returns_empty_when_audio_missing(self, tmp_path: Path) -> None:
        """Returns empty when audio missing."""
        # CUE references a file that doesn't exist and no other audio in dir
        cue_item = _make_cue_item(
            tmp_path,
            'FILE "missing.flac" WAVE\n  TRACK 01 AUDIO\n    TITLE "x"\n    INDEX 01 00:00:00\n',
        )
        provider = _make_provider(base_path=str(tmp_path))
        provider._parse_album = AsyncMock(return_value=None)  # type: ignore[method-assign]
        tracks = await provider._cue.parse_tracks(cue_item)
        assert tracks == []
        provider.logger.error.assert_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_builds_tracks_with_cue_metadata(self, tmp_path: Path) -> None:
        """Builds tracks with cue metadata."""
        # CUE with 3 tracks, audio file exists, audio tags have a different album/artist
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        # audio has different album+albumartist+year — CUE should override
        tags = _make_audio_tags(
            duration=900.0,
            album="Different Album From Tag",
            albumartist="Different Artist From Tag",
        )
        album = Album(
            item_id="a1",
            provider=provider.instance_id,
            name="Live at the BBC",
            provider_mappings=set(),
        )
        self._wire_provider_for_parse(provider, album)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert len(tracks) == 3
        # CUE TITLE overrode audio tag for album name
        assert tags.tags["album"] == "Live at the BBC"
        # CUE top-level PERFORMER overrode audio albumartist
        assert tags.tags["albumartist"] == "Dire Straits"
        # per-track names from CUE
        assert tracks[0].name == "Down to the Waterline"
        assert tracks[1].name == "Six Blade Knife"
        assert tracks[2].name == "Water of Love"
        # track numbers preserved
        assert [t.track_number for t in tracks] == [1, 2, 3]
        # item_ids are synthetic and distinct
        ids = [t.item_id for t in tracks]
        assert len(set(ids)) == 3
        for track, num in zip(tracks, [1, 2, 3], strict=True):
            assert track.item_id == make_cue_track_id(cue_item.relative_path, num)
        # ISRC from CUE propagates to each track
        for track in tracks:
            isrcs = [v for k, v in track.external_ids if k == ExternalID.ISRC]
            assert len(isrcs) == 1
            assert isrcs[0].startswith("GBAMU78")

    @pytest.mark.asyncio
    async def test_track_durations(self, tmp_path: Path) -> None:
        """Track durations."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        # total_duration = 900s (15min)
        tags = _make_audio_tags(duration=900.0, album="Live at the BBC")
        self._wire_provider_for_parse(provider)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        # Track 1: 00:00:00 to 04:10:40 = ~250.53s
        # Track 2: 04:10:40 to 07:58:05 = ~227.4s
        # Track 3: 07:58:05 to 900 = ~421.93s
        assert tracks[0].duration == round(4 * 60 + 10 + 40 / 75)
        assert tracks[1].duration == round((7 * 60 + 58 + 5 / 75) - (4 * 60 + 10 + 40 / 75))
        assert tracks[2].duration == round(900.0 - (7 * 60 + 58 + 5 / 75))

    @pytest.mark.asyncio
    async def test_honors_disc_number_tag(self, tmp_path: Path) -> None:
        """Honors disc number tag."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        tags = _make_audio_tags(duration=900.0, album="Live at the BBC", disc="2")
        self._wire_provider_for_parse(provider)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert all(t.disc_number == 2 for t in tracks)

    @pytest.mark.asyncio
    async def test_skips_track_missing_title(self, tmp_path: Path) -> None:
        """Skips track missing title."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_text = (
            'PERFORMER "X"\n'
            'TITLE "Album"\n'
            'FILE "album.flac" WAVE\n'
            "  TRACK 01 AUDIO\n"
            '    TITLE "Real Track"\n'
            "    INDEX 01 00:00:00\n"
            "  TRACK 02 AUDIO\n"
            "    INDEX 01 02:00:00\n"  # no TITLE
        )
        cue_item = _make_cue_item(tmp_path, cue_text)
        provider = _make_provider(base_path=str(tmp_path))
        tags = _make_audio_tags(duration=300.0, album="Album")
        self._wire_provider_for_parse(provider)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert len(tracks) == 1
        assert tracks[0].name == "Real Track"
        # a warning should have been emitted for the skipped track
        warning_msgs = [str(c) for c in provider.logger.warning.call_args_list]  # type: ignore[attr-defined]
        assert any("TITLE" in msg for msg in warning_msgs)

    @pytest.mark.asyncio
    async def test_track_artist_falls_back_to_album_performer(self, tmp_path: Path) -> None:
        """Track artist falls back to album performer."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        # no per-track PERFORMER, only top-level
        cue_text = (
            'PERFORMER "Band"\n'
            'TITLE "Album"\n'
            'FILE "album.flac" WAVE\n'
            "  TRACK 01 AUDIO\n"
            '    TITLE "T1"\n'
            "    INDEX 01 00:00:00\n"
        )
        cue_item = _make_cue_item(tmp_path, cue_text)
        provider = _make_provider(base_path=str(tmp_path))
        tags = _make_audio_tags(duration=300.0, album="Album")
        self._wire_provider_for_parse(provider)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert len(tracks) == 1
        assert [a.name for a in tracks[0].artists] == ["Band"]


class TestGetStreamDetailsForCueTrack:
    """Tests for _get_stream_details_for_cue_track."""

    @pytest.mark.asyncio
    async def test_invalid_id_raises(self) -> None:
        """Invalid id raises."""
        provider = _make_provider()
        with pytest.raises(InvalidDataError):
            await provider._cue.get_stream_details("not_a_cue_id.flac")

    @pytest.mark.asyncio
    async def test_missing_audio_raises(self, tmp_path: Path) -> None:
        """Missing audio raises."""
        cue_item = _make_cue_item(
            tmp_path,
            'FILE "missing.flac" WAVE\n  TRACK 01 AUDIO\n    TITLE "x"\n    INDEX 01 00:00:00\n',
        )
        provider = _make_provider(base_path=str(tmp_path))
        provider.resolve = AsyncMock(return_value=cue_item)  # type: ignore[method-assign]
        item_id = make_cue_track_id(cue_item.relative_path, 1)
        with pytest.raises(MediaNotFoundError):
            await provider._cue.get_stream_details(item_id)

    @pytest.mark.asyncio
    async def test_unknown_track_number_raises(self, tmp_path: Path) -> None:
        """Unknown track number raises."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        provider.resolve = AsyncMock(return_value=cue_item)  # type: ignore[method-assign]
        # request track 99 which isn't in the CUE
        item_id = make_cue_track_id(cue_item.relative_path, 99)
        with (
            patch(
                "music_assistant.providers.filesystem_local.cue.async_parse_tags",
                AsyncMock(return_value=_make_audio_tags(duration=900.0)),
            ),
            pytest.raises(MediaNotFoundError),
        ):
            await provider._cue.get_stream_details(item_id)

    @pytest.mark.asyncio
    async def test_builds_streamdetails_with_offset_and_duration(self, tmp_path: Path) -> None:
        """Builds streamdetails with offset and duration."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        provider.resolve = AsyncMock(return_value=cue_item)  # type: ignore[method-assign]
        item_id = make_cue_track_id(cue_item.relative_path, 2)
        tags = _make_audio_tags(duration=900.0)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            details = await provider._cue.get_stream_details(item_id)

        assert isinstance(details, StreamDetails)
        assert details.item_id == item_id
        assert details.can_seek is True
        assert details.allow_seek is True
        assert details.audio_format.content_type == ContentType.PCM_F32LE
        assert details.data is not None
        assert details.data["audio_path"] == str(audio_file)
        # Track 2 starts at 04:10:40 = 250.533...
        expected_start = 4 * 60 + 10 + 40 / 75
        assert abs(details.data["start_seconds"] - expected_start) < 0.001


class TestGetAudioStreamErrorPath:
    """Tests for get_audio_stream error handling."""

    @pytest.mark.asyncio
    async def test_invalid_streamdetails_raises(self) -> None:
        """Invalid streamdetails raises."""
        provider = _make_provider()
        bad_details = MagicMock(spec=StreamDetails)
        bad_details.data = None
        bad_details.item_id = "whatever"
        with pytest.raises(InvalidDataError):
            async for _ in provider._cue.get_audio_stream(bad_details):
                pass


class TestProcessDeletionsCueBranch:
    """Tests for _process_deletions routing of CUE-derived track ids."""

    @pytest.mark.asyncio
    async def test_cue_track_id_routed_to_track_controller(self) -> None:
        """Cue track id routed to track controller."""
        provider = _make_provider()
        controller = MagicMock()
        controller.get_library_item_by_prov_id = AsyncMock(return_value=None)
        provider.mass.music.get_controller = MagicMock(return_value=controller)  # type: ignore[method-assign]

        cue_track_id = make_cue_track_id("artist/album.cue", 3)
        await provider._process_deletions({cue_track_id})

        # must have consulted a controller (the track controller)
        assert provider.mass.music.get_controller.called


class TestGetTrackCueBranch:
    """Test get_track's CUE-id branch."""

    @pytest.mark.asyncio
    async def test_returns_matching_cue_track(self, tmp_path: Path) -> None:
        """Returns matching cue track."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        provider.resolve = AsyncMock(return_value=cue_item)  # type: ignore[method-assign]

        # mock _parse_cue_tracks to return three synthetic tracks
        def fake_track(num: int) -> Track:
            return Track(
                item_id=make_cue_track_id(cue_item.relative_path, num),
                provider=provider.instance_id,
                name=f"Track {num}",
                provider_mappings=set(),
            )

        provider._cue.parse_tracks = AsyncMock(  # type: ignore[method-assign]
            return_value=[fake_track(1), fake_track(2), fake_track(3)]
        )
        item_id = make_cue_track_id(cue_item.relative_path, 2)
        track = await provider.get_track(item_id)
        assert track.item_id == item_id
        assert track.name == "Track 2"

    @pytest.mark.asyncio
    async def test_missing_cue_track_raises(self, tmp_path: Path) -> None:
        """Missing cue track raises."""
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        provider.resolve = AsyncMock(return_value=cue_item)  # type: ignore[method-assign]
        provider._cue.parse_tracks = AsyncMock(return_value=[])  # type: ignore[method-assign]
        item_id = make_cue_track_id(cue_item.relative_path, 1)
        with pytest.raises(MediaNotFoundError):
            await provider.get_track(item_id)
