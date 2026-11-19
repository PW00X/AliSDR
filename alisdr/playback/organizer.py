"""Recording sorting: moves loose .wav files from the scanner's root output
directory into date subfolders (local time embedded in the filename)."""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List
from urllib.parse import unquote

from alisdr.playback.config import DATE_DIR_FORMAT, FILENAME_REGEX, ALISDR_DIR
from alisdr.playback.models import MANUAL_FREQ_KHZ

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(FILENAME_REGEX)


@dataclass
class ParsedName:
    dt_local: datetime
    freq_khz: int
    station_name: str = ""


def parse_filename(name: str) -> ParsedName | None:
    """Extracts the date/time (local time) and frequency (kHz) from a
    filename. Microseconds (see FILENAME_REGEX) are optional - filenames
    from before that change still parse, just with zero microseconds."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    year, month, day, hour, minute, second, micros, freq, station, label = m.groups()
    dt_local = datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second),
        microsecond=int(micros) if micros else 0,
    ).astimezone()  # the recorder names files in this computer's local time (recorder.py)
    # freq is None for a "..._session.wav" name (manual/"Single File" mode -
    # see FILENAME_REGEX's own comment) - no single fixed frequency, so
    # MANUAL_FREQ_KHZ (0, never a real radio frequency) stands in for it.
    freq_khz = int(freq) if freq is not None else MANUAL_FREQ_KHZ
    return ParsedName(dt_local=dt_local, freq_khz=freq_khz, station_name=unquote(station or label or ""))


def parse_recording(path: Path) -> ParsedName | None:
    """Name-only files retain frequency in their portable audio metadata."""
    from alisdr.recording_names import read_recording_metadata
    parsed = parse_filename(path.name)
    if parsed is None:
        return None
    metadata = read_recording_metadata(path)
    frequency = metadata.get('freq_hz')
    if isinstance(frequency, (int, float)) and not isinstance(frequency, bool) and 0 < frequency <= 6_000_000_000:
        parsed.freq_khz = round(frequency / 1000)
        if isinstance(metadata.get('recording_name'), str):
            parsed.station_name = metadata['recording_name']
    if parsed.station_name and not parsed.freq_khz:
        return None  # a name without identity cannot safely join a frequency group
    return parsed


def organize(root: Path = ALISDR_DIR) -> List[Path]:
    """Moves .wav files sitting loose directly in `root` into
    `root/YYYY-MM-DD/` subfolders matching the local date from the filename.

    Returns the list of moved files' new paths. Safe to run repeatedly
    (idempotent) - files already in a date subfolder are left alone.
    """
    root = Path(root)
    if not root.is_dir():
        logger.warning("Directory %s doesn't exist", root)
        return []

    moved: List[Path] = []

    loose_files = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in (".wav", ".mp3")]

    for path in sorted(loose_files):
        parsed = parse_recording(path)
        if parsed is None:
            logger.debug("Skipping file with an unrecognized name: %s", path.name)
            continue

        date_dir = root / parsed.dt_local.strftime(DATE_DIR_FORMAT)
        date_dir.mkdir(exist_ok=True)
        dest = date_dir / path.name

        if dest.exists():
            if dest.resolve() == path.resolve():
                continue
            # Name already taken by another file (shouldn't normally happen -
            # the timestamp in the name has second resolution) - append a
            # suffix instead of overwriting or losing data.
            stem, suffix = dest.stem, dest.suffix
            i = 1
            while dest.exists():
                dest = date_dir / f"{stem}_dup{i}{suffix}"
                i += 1
            logger.warning("Name collision, saving as %s", dest.name)

        shutil.move(str(path), str(dest))
        moved.append(dest)
        logger.info("Moved %s -> %s", path.name, dest.relative_to(root))

    return moved


def iter_all_recordings(root: Path = ALISDR_DIR):
    """Returns the paths of every .wav/.mp3 file in the root directory and
    every date subfolder (sorted by name, i.e. chronologically)."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(list(root.glob("**/*.wav")) + list(root.glob("**/*.mp3")))
