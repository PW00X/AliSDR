"""Readable recording filenames with portable identity in WAV/MP3 metadata."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .i18n import tr
from .wav_metadata import append_metadata_chunk, read_metadata_chunk

_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d{6})?)_.+\.(wav|mp3)$")


def channel_label(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*%\x00-\x1f]', lambda m: f"%{ord(m[0]):02X}", name.strip()[:48])


def named_recording_path(path: Path, name: str) -> Path:
    match = _STAMP.fullmatch(path.name)
    if not match or not name.strip():
        return path
    return path.with_name(f"{match[1]}_{channel_label(name)}.{match[2]}")


def read_recording_metadata(path: Path) -> dict:
    if path.suffix.lower() == '.wav':
        return read_metadata_chunk(path) or {}
    from mutagen.id3 import ID3, ID3NoHeaderError, error
    try:
        tags = ID3(path)
        frames = ([f for f in tags.getall('TXXX') if f.desc == 'AliSDR'] + tags.getall('COMM')
                  + [f for f in tags.getall('TXXX') if f.desc.casefold() == 'comment'])
        for frame in frames:
            for text in frame.text:
                try:
                    value = json.loads(text)
                    if isinstance(value, dict):
                        return value
                except (ValueError, TypeError):
                    continue
    except (OSError, ID3NoHeaderError, error):
        pass
    return {}


def ensure_recording_identity(path: Path, freq_hz: int, name: str, *, new_file: bool = False) -> None:
    if path.suffix.lower() == '.wav':
        with path.open('rb') as stream:
            header = stream.read(12)
        if header[:4] != b'RIFF' or header[8:12] != b'WAVE':
            raise ValueError(tr('Invalid WAV recording'))
    metadata = read_recording_metadata(path)
    name = name.strip()[:48]
    if metadata.get('freq_hz') == freq_hz and metadata.get('recording_name') == name:
        return
    metadata.update(freq_hz=freq_hz, recording_name=name)
    # Freshly closed WAVs are still private to the recorder. Existing files
    # are updated through a temporary copy, so errors leave the original intact.
    if new_file and path.suffix.lower() == '.wav':
        append_metadata_chunk(path, metadata)
        return
    st = path.stat()
    fd, temporary = tempfile.mkstemp(prefix='.alisdr-name-', suffix='.part', dir=path.parent)
    os.close(fd)
    temp = Path(temporary)
    try:
        shutil.copyfile(path, temp)
        if path.suffix.lower() == '.wav':
            append_metadata_chunk(temp, metadata)
        else:
            from mutagen.id3 import ID3, ID3NoHeaderError, TXXX
            try:
                tags = ID3(temp)
            except ID3NoHeaderError:
                tags = ID3()
            tags.add(TXXX(encoding=3, desc='AliSDR', text=[json.dumps(metadata, ensure_ascii=False)]))
            tags.save(temp)
        shutil.copystat(path, temp)
        # Do not replace a file another process changed while it was copied.
        current = path.stat()
        if (current.st_size, current.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            raise OSError(tr('Recording changed while its metadata was being updated'))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _reserve_destination(target: Path):
    # Reserve the destination exclusively. Never overwrite an existing recording.
    with target.open('xb'):
        pass
    reserved = target.stat()
    try:
        yield reserved
    except BaseException:
        if target.exists() and target.stat().st_ino == reserved.st_ino and target.stat().st_size == 0:
            target.unlink()
        raise


def move_without_overwrite(path: Path, target: Path) -> Path:
    if target == path:
        return path
    with _reserve_destination(target):
        path.replace(target)
    return target


def rename_recording(path: Path, freq_hz: int, name: str, *, new_file: bool = False) -> Path:
    target = named_recording_path(path, name)
    if target == path:
        ensure_recording_identity(path, freq_hz, name, new_file=new_file)
        return path
    with _reserve_destination(target) as reserved:
        ensure_recording_identity(path, freq_hz, name, new_file=new_file)
        if target.stat().st_ino != reserved.st_ino:
            raise FileExistsError(target)
        path.replace(target)
    return target
