"""In-process DAB Mode I decoding through the AliSDR native library."""
from __future__ import annotations

import ctypes as C
from pathlib import Path
from queue import Empty, Full, Queue

import numpy as np
from scipy.signal import ellip, sosfilt

from .dsp import StreamResampler

from .i18n import tr

SAMPLE_RATE = 2_048_000
AUDIO_RATE = 48_000
CHANNELS = {
    f"{band}{letter}": (start + step * 1712) * 1000
    for band, start in enumerate((174928, 181936, 188928, 195936, 202928, 209936, 216928, 223936), 5)
    for step, letter in enumerate("ABCD")
}
CHANNELS.update(zip(("13" + letter for letter in "ABCDEF"),
                    (230784000, 232496000, 234208000, 235776000, 237488000, 239200000)))

_STATION = C.CFUNCTYPE(None, C.c_uint32, C.c_char_p, C.c_int)
_AUDIO = C.CFUNCTYPE(None, C.POINTER(C.c_int16), C.c_int, C.c_int)
_RECEPTION = C.CFUNCTYPE(None, C.c_bool, C.c_int16, C.c_int32)
_METADATA = C.CFUNCTYPE(None, C.c_uint32, C.c_char_p)
_IMAGE = C.CFUNCTYPE(None, C.c_uint32, C.POINTER(C.c_uint8), C.c_int)
_LIBRARY = Path(__file__).resolve().parent.parent / ".venv" / "lib" / "libalisdr_dab.so"


class ChannelInput:
    """Condition a continuous stream before the decoder looks for the OFDM null."""

    def __init__(self, rate: float):
        self._resample = StreamResampler(rate, SAMPLE_RATE) if rate != SAMPLE_RATE else None
        # Keep the occupied ±768 kHz and some tuner-error margin. Noise and
        # adjacent emissions outside the ensemble can obscure its quiet null.
        self._filter = ellip(7, 0.2, 50, 810_000, fs=SAMPLE_RATE, output="sos").astype(np.float32)
        self._state = np.zeros((len(self._filter), 2), dtype=np.complex64)

    def process(self, iq: np.ndarray) -> np.ndarray:
        # There is no DAB carrier at DC. USB capture can have a centre spike
        # large enough to hide the null even when the wanted ensemble is strong.
        iq = iq - iq.mean(dtype=np.complex64)
        if self._resample is not None:
            iq = self._resample.process(iq)
        if iq.size:
            iq, self._state = sosfilt(self._filter, iq, zi=self._state)
        return iq


def load_decoder():
    try:
        path = _LIBRARY
        if not path.exists():
            legacy = path.with_name("libfalconsdr_dab.so")
            if legacy.is_file():
                path = legacy
        library = C.CDLL(str(path))
        library.falcon_dab_open.argtypes = [_STATION, _AUDIO, _RECEPTION]
        library.falcon_dab_open.restype = C.c_void_p
        library.falcon_dab_close.argtypes = [C.c_void_p]
        library.falcon_dab_close.restype = None
        library.falcon_dab_push.argtypes = [C.c_void_p, C.c_void_p, C.c_int]
        library.falcon_dab_push.restype = C.c_int
        library.falcon_dab_select.argtypes = [C.c_void_p, C.c_uint32]
        library.falcon_dab_select.restype = None
        if hasattr(library, "falcon_dab_set_metadata"):
            library.falcon_dab_set_metadata.argtypes = [C.c_void_p, _METADATA]
            library.falcon_dab_set_metadata.restype = None
        if hasattr(library, "falcon_dab_set_image"):
            library.falcon_dab_set_image.argtypes = [C.c_void_p, _IMAGE]
            library.falcon_dab_set_image.restype = None
        return library
    except (OSError, AttributeError) as exc:
        raise RuntimeError(tr("DAB decoder unavailable. Run the receiver installer with --server --with-dab.")
                           + f" ({exc})") from exc


class Decoder:
    def __init__(self, library, on_station, on_reception, on_metadata=None, on_image=None):
        self.library = library
        self.audio: Queue = Queue(maxsize=12)
        self.callbacks = (_STATION(on_station), _AUDIO(self._audio), _RECEPTION(on_reception))
        self.handle = library.falcon_dab_open(*self.callbacks)
        if not self.handle:
            raise RuntimeError(tr("The DAB decoder could not start."))
        if on_metadata is not None and hasattr(library, "falcon_dab_set_metadata"):
            self.metadata_callback = _METADATA(on_metadata)
            library.falcon_dab_set_metadata(self.handle, self.metadata_callback)

        if on_image is not None and hasattr(library, "falcon_dab_set_image"):
            def image(sid, pointer, size):
                if pointer and 0 < size <= 512 * 1024:
                    on_image(sid, C.string_at(pointer, size))

            self.image_callback = _IMAGE(image)
            library.falcon_dab_set_image(self.handle, self.image_callback)

    def _audio(self, pointer, count, rate):
        if count <= 0 or count % 2 or not 8_000 <= rate <= 96_000:
            return
        pcm = np.ctypeslib.as_array(pointer, shape=(count,)).copy()
        try:
            self.audio.put_nowait((rate, pcm))
        except Full:
            pass

    def drain_audio(self):
        while True:
            try:
                yield self.audio.get_nowait()
            except Empty:
                return

    def select(self, sid):
        self.library.falcon_dab_select(self.handle, sid)
        for _ in self.drain_audio():
            pass

    def push(self, iq):
        iq = np.ascontiguousarray(iq, dtype=np.complex64)
        if self.library.falcon_dab_push(self.handle, iq.ctypes.data, iq.size) != iq.size:
            raise RuntimeError(tr("The DAB decoder could not keep up with the receiver."))

    def close(self):
        self.library.falcon_dab_close(self.handle)
