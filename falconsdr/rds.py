"""Decode FM RDS programme identification, service name and RadioText."""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt

from .dsp import StreamResampler

_RATE = 19000  # 16 samples per RDS symbol
_OFFSETS = (0x0FC, 0x198, 0x168, 0x350, 0x1B4)
_BIT_WEIGHTS = 1 << np.arange(25, -1, -1, dtype=np.uint32)
_CHECK = np.arange(65536, dtype=np.uint32) << 10
for bit in range(25, 9, -1):
    _CHECK ^= ((_CHECK >> bit) & 1) * (0x5B9 << (bit - 10))


class RDSDecoder:
    def __init__(self):
        self.info = {}
        self._rate = 0
        self._index = 0
        self._buffer = np.empty(0, dtype=np.complex64)
        self._filter = butter(4, 2400, fs=_RATE, output="sos")
        self._zi = np.zeros((len(self._filter), 2), dtype=np.complex128)
        self._ps = {}
        self._rt = {}
        self._rt_kind = None

    def feed(self, composite: np.ndarray, rate: int) -> None:
        if self._rate != rate:
            self._rate = rate
            self._resampler = StreamResampler(rate, _RATE)
        phase = (self._index + np.arange(composite.size)) * (-2j * np.pi * 57000 / rate)
        self._index = (self._index + composite.size) % rate
        mixed = self._resampler.process(composite * np.exp(phase))
        if not mixed.size:
            return
        filtered, self._zi = sosfilt(self._filter, mixed, zi=self._zi)
        self._buffer = np.concatenate((self._buffer, filtered))
        if self._buffer.size < _RATE // 2:
            return
        self._decode(self._buffer)
        # Preserve enough overlap to recover any group straddling two windows.
        self._buffer = self._buffer[-2048:]

    def _decode(self, samples):
        groups = {}
        for phase in range(16):
            count = (samples.size - phase) // 16
            symbols = samples[phase:phase + count * 16].reshape(-1, 16)
            matched = (symbols[:, :8] - symbols[:, 8:]).sum(axis=1)
            bits = (matched[1:] * matched[:-1].conj()).real < 0
            words = np.lib.stride_tricks.sliding_window_view(bits, 26) @ _BIT_WEIGHTS
            payload = words >> 10
            crc = _CHECK[payload] ^ (words & 1023)
            a, b, c, cp, d = _OFFSETS
            version_b = (payload[26:-52] & 0x0800) != 0
            valid = ((crc[:-78] == a) & (crc[26:-52] == b)
                     & (crc[52:-26] == np.where(version_b, cp, c)) & (crc[78:] == d))
            for start in np.flatnonzero(valid):
                # Nearby timing phases decode the same group. Keep each once,
                # while accepting repeated groups later in the message.
                position = (int(start) * 16 + phase + 8) // 16
                groups[position] = tuple(int(payload[start + i]) for i in (0, 26, 52, 78))
        previous = None
        for position, group in sorted(groups.items()):
            if previous and position - previous[0] <= 1 and group == previous[1]:
                continue
            self._group(*group)
            previous = (position, group)

    def _group(self, pi, header, block_c, block_d):
        programme = f"{pi:04X}"
        if self.info.get("pi") != programme:
            self.info.clear()
            self.info["pi"] = programme
            self._ps.clear()
            self._rt.clear()
            self._rt_kind = None
        kind = header >> 12
        if kind == 0:
            self._ps[header & 3] = block_d.to_bytes(2, "big")
            if len(self._ps) == 4:
                self.info["name"] = self._text(b"".join(self._ps[i] for i in range(4)))
        elif kind == 2:
            version_b = bool(header & 0x0800)
            text_kind = (version_b, bool(header & 0x0010))
            if text_kind != self._rt_kind:
                self._rt_kind = text_kind
                self._rt.clear()
                self.info.pop("text", None)
            chars = block_d.to_bytes(2, "big")
            if not version_b:
                chars = block_c.to_bytes(2, "big") + chars
            self._rt[header & 15] = chars
            contiguous = bytearray()
            for index in range(16):
                if index not in self._rt:
                    break
                contiguous.extend(self._rt[index])
            if b"\r" in contiguous or len(self._rt) == 16:
                self.info["text"] = self._text(contiguous.split(b"\r", 1)[0])

    @staticmethod
    def _text(raw):
        return "".join(chr(byte) if 32 <= byte < 127 else " " for byte in raw).strip()
