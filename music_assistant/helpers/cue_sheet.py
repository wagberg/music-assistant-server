"""CUE sheet parser for Music Assistant.

Parses standard CUE sheet format into structured data.
Supports PERFORMER, TITLE, FILE, TRACK, INDEX, ISRC, and REM commands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class CueTrack:
    """A single track entry from a CUE sheet."""

    number: int
    title: str | None = None
    performer: str | None = None
    start_position: float = 0.0  # seconds from INDEX 01
    isrc: str | None = None
    musicbrainz_trackid: str | None = None


@dataclass
class CueSheet:
    """Parsed CUE sheet data."""

    file_path: str | None = None  # referenced audio file (None for embedded CUE)
    title: str | None = None  # album title
    performer: str | None = None  # album artist
    date: str | None = None  # year from REM DATE
    genre: str | None = None  # from REM GENRE
    musicbrainz_albumid: str | None = None  # from REM MUSICBRAINZ_ALBUMID
    tracks: list[CueTrack] = field(default_factory=list)


def _parse_timestamp(timestamp: str) -> float:
    """Convert CUE timestamp (MM:SS:FF) to seconds.

    :param timestamp: CUE format timestamp where FF = frames at 75fps.
    """
    match = re.match(r"(\d+):(\d+):(\d+)", timestamp)
    if not match:
        return 0.0
    minutes, seconds, frames = int(match.group(1)), int(match.group(2)), int(match.group(3))
    return minutes * 60.0 + seconds + frames / 75.0


def _unquote(value: str) -> str:
    """Remove surrounding quotes from a CUE value."""
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def parse_cue_sheet(cue_content: str) -> CueSheet:
    """Parse CUE sheet content into structured data.

    :param cue_content: The raw text content of a CUE sheet.
    """
    sheet = CueSheet()
    current_track: CueTrack | None = None

    for raw_line in cue_content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        upper_line = line.upper()

        if upper_line.startswith("REM "):
            _parse_rem_line(line, sheet, current_track)

        elif upper_line.startswith("PERFORMER "):
            value = _unquote(line[10:])
            if current_track is not None:
                current_track.performer = value
            else:
                sheet.performer = value

        elif upper_line.startswith("TITLE "):
            value = _unquote(line[6:])
            if current_track is not None:
                current_track.title = value
            else:
                sheet.title = value

        elif upper_line.startswith("FILE "):
            # FILE "filename.flac" WAVE
            # extract filename between quotes, ignore type
            match = re.match(r'FILE\s+"([^"]+)"', line, re.IGNORECASE)
            if match:
                sheet.file_path = match.group(1)
            else:
                # handle unquoted filename: FILE filename.flac WAVE
                parts = line.split(None, 2)
                if len(parts) >= 2:
                    sheet.file_path = parts[1]

        elif upper_line.startswith("TRACK "):
            # TRACK 01 AUDIO
            match = re.match(r"TRACK\s+(\d+)", line, re.IGNORECASE)
            if match:
                current_track = CueTrack(number=int(match.group(1)))
                sheet.tracks.append(current_track)

        elif upper_line.startswith("INDEX "):
            if current_track is None:
                continue
            # INDEX 01 MM:SS:FF — use INDEX 01 as track start
            match = re.match(r"INDEX\s+(\d+)\s+(\d+:\d+:\d+)", line, re.IGNORECASE)
            if match and match.group(1) == "01":
                current_track.start_position = _parse_timestamp(match.group(2))

        elif upper_line.startswith("ISRC "):
            if current_track is not None:
                current_track.isrc = _unquote(line[5:])

    return sheet


def _parse_rem_line(line: str, sheet: CueSheet, current_track: CueTrack | None) -> None:
    """Parse a REM line for metadata.

    :param line: The full REM line.
    :param sheet: The CueSheet being built.
    :param current_track: The current track context, if any.
    """
    # REM KEY VALUE — split into at most 3 parts
    parts = line.split(None, 2)
    if len(parts) < 3:
        return

    key = parts[1].upper()
    value = _unquote(parts[2])

    if key == "DATE":
        sheet.date = value
    elif key == "GENRE":
        sheet.genre = value
    elif key == "MUSICBRAINZ_ALBUMID":
        sheet.musicbrainz_albumid = value
    elif key == "MUSICBRAINZ_TRACKID" and current_track is not None:
        current_track.musicbrainz_trackid = value
    elif key == "ISRC" and current_track is not None:
        current_track.isrc = value
