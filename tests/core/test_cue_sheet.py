"""Tests for CUE sheet parser."""

from music_assistant.helpers.cue_sheet import parse_cue_sheet

SAMPLE_CUE = """\
REM GENRE Rock
REM DATE 1995
REM MUSICBRAINZ_ALBUMID 4591f427-4632-474c-9f2d-b4f009e53096
PERFORMER "The Artist"
TITLE "The Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "First Track"
    PERFORMER "The Artist"
    ISRC USRC17607839
    REM MUSICBRAINZ_TRACKID d5c4a1b0-1234-5678-9abc-def012345678
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second Track"
    PERFORMER "Featured Artist"
    INDEX 00 03:45:50
    INDEX 01 03:46:00
  TRACK 03 AUDIO
    TITLE "Third Track"
    INDEX 01 07:30:25
"""


def test_parse_basic_cue_sheet() -> None:
    """Test parsing a standard CUE sheet."""
    result = parse_cue_sheet(SAMPLE_CUE)

    assert result.title == "The Album"
    assert result.performer == "The Artist"
    assert result.file_path == "album.flac"
    assert result.date == "1995"
    assert result.genre == "Rock"
    assert result.musicbrainz_albumid == "4591f427-4632-474c-9f2d-b4f009e53096"
    assert len(result.tracks) == 3


def test_parse_track_metadata() -> None:
    """Test track-level metadata parsing."""
    result = parse_cue_sheet(SAMPLE_CUE)

    track1 = result.tracks[0]
    assert track1.number == 1
    assert track1.title == "First Track"
    assert track1.performer == "The Artist"
    assert track1.isrc == "USRC17607839"
    assert track1.musicbrainz_trackid == "d5c4a1b0-1234-5678-9abc-def012345678"

    track2 = result.tracks[1]
    assert track2.number == 2
    assert track2.title == "Second Track"
    assert track2.performer == "Featured Artist"


def test_parse_timestamps() -> None:
    """Test CUE timestamp to seconds conversion."""
    result = parse_cue_sheet(SAMPLE_CUE)

    # Track 1: INDEX 01 00:00:00 = 0.0 seconds
    assert result.tracks[0].start_position == 0.0

    # Track 2: INDEX 01 03:46:00 = 3*60 + 46 + 0/75 = 226.0 seconds
    # (INDEX 00 at 03:45:50 should be ignored, INDEX 01 is used)
    assert result.tracks[1].start_position == 226.0

    # Track 3: INDEX 01 07:30:25 = 7*60 + 30 + 25/75 = 450.333... seconds
    assert abs(result.tracks[2].start_position - (7 * 60 + 30 + 25 / 75)) < 0.001


def test_parse_empty_cue() -> None:
    """Test parsing an empty CUE sheet."""
    result = parse_cue_sheet("")
    assert result.tracks == []
    assert result.title is None
    assert result.file_path is None


def test_parse_minimal_cue() -> None:
    """Test parsing a minimal CUE sheet with just tracks."""
    cue = """\
FILE "music.mp3" MP3
  TRACK 01 AUDIO
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    INDEX 01 05:00:00
"""
    result = parse_cue_sheet(cue)

    assert result.file_path == "music.mp3"
    assert len(result.tracks) == 2
    assert result.tracks[0].number == 1
    assert result.tracks[0].title is None
    assert result.tracks[0].start_position == 0.0
    assert result.tracks[1].number == 2
    assert result.tracks[1].start_position == 300.0  # 5 minutes


def test_parse_unquoted_filename() -> None:
    """Test parsing FILE command with unquoted filename."""
    cue = """\
FILE music.flac WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
"""
    result = parse_cue_sheet(cue)
    assert result.file_path == "music.flac"


def test_parse_no_performer() -> None:
    """Test parsing CUE sheet without PERFORMER tags."""
    cue = """\
TITLE "My Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Song One"
    INDEX 01 00:00:00
"""
    result = parse_cue_sheet(cue)
    assert result.performer is None
    assert result.tracks[0].performer is None
    assert result.tracks[0].title == "Song One"


def test_track_without_title_defaults_none() -> None:
    """Test that tracks without TITLE have None as title."""
    cue = """\
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
"""
    result = parse_cue_sheet(cue)
    assert result.tracks[0].title is None


def test_rem_lines_at_track_level() -> None:
    """Test REM lines within track context."""
    cue = """\
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Track One"
    REM MUSICBRAINZ_TRACKID abc-123
    REM ISRC GBAYE0000351
    INDEX 01 00:00:00
"""
    result = parse_cue_sheet(cue)
    assert result.tracks[0].musicbrainz_trackid == "abc-123"
    assert result.tracks[0].isrc == "GBAYE0000351"
