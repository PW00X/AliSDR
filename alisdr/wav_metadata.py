"""Embeds the scanner's settings at recording time (gain, trigger, window,
...) as JSON in a standard RIFF ``LIST``/``INFO``/``ICMT`` chunk - players
skip it, ffprobe/exiftool show it.

Call only after ``wave.Wave_write.close()``, or the chunk sizes are stale.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any


def append_metadata_chunk(path: Path, metadata: dict[str, Any]) -> None:
    """Appends the metadata chunk and fixes up the master RIFF chunk's
    overall size field (bytes 4:8, "file size - 8") to include it."""
    payload = json.dumps(metadata, ensure_ascii=False, default=str).encode("utf-8")
    if len(payload) % 2:
        payload += b"\x00"  # RIFF (sub)chunks must be word-aligned
    icmt = b"ICMT" + struct.pack("<I", len(payload)) + payload
    list_data = b"INFO" + icmt
    chunk = b"LIST" + struct.pack("<I", len(list_data)) + list_data

    with path.open("r+b") as f:
        f.seek(0, 2)
        f.write(chunk)
        file_size = f.tell()
        f.seek(4)
        f.write(struct.pack("<I", file_size - 8))


def read_metadata_chunk(path: Path) -> dict[str, Any] | None:
    """Read INFO comments without loading or scanning the audio payload."""
    result = None
    try:
        with path.open('rb') as stream:
            header = stream.read(12)
            if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WAVE':
                return None
            end = path.stat().st_size
            while stream.tell() + 8 <= end:
                chunk, size = struct.unpack('<4sI', stream.read(8))
                start = stream.tell()
                if start + size > end:
                    break
                if chunk == b'LIST' and size <= 1024 * 1024:
                    data = stream.read(size)
                    if data[:4] == b'INFO':
                        offset = 4
                        while offset + 8 <= len(data):
                            key, length = struct.unpack_from('<4sI', data, offset)
                            offset += 8
                            if offset + length > len(data):
                                break
                            if key == b'ICMT':
                                try:
                                    value = json.loads(data[offset:offset + length].rstrip(b'\x00').decode('utf-8'))
                                    if isinstance(value, dict):
                                        result = value
                                except (UnicodeDecodeError, ValueError):
                                    pass
                            offset += length + length % 2
                stream.seek(start + size + size % 2)
    except (OSError, struct.error):
        return None
    return result
