"""Verify the headless server runtime and its installed dependencies."""
from __future__ import annotations

import sys
from pathlib import Path


def _checks():
    def numpy_scipy():
        import numpy
        import scipy.signal
        return f"numpy {numpy.__version__}, scipy.signal ok ({scipy.signal.resample_poly.__name__})"

    def timezones():
        from zoneinfo import ZoneInfo
        ZoneInfo("Europe/Warsaw")
        return "Europe/Warsaw ok"

    def rtlsdr_library():
        from alisdr import sdr_backend
        if not sdr_backend.HAVE_RTLSDR_NATIVE:
            raise RuntimeError("pyrtlsdr/librtlsdr could not be loaded")
        import rtlsdr
        return f"librtlsdr loaded ({Path(rtlsdr.__file__).parent.name})"

    def native_receiver_libraries():
        from alisdr import sdr_native
        hack, air = sdr_native.hackrf_library(), sdr_native.airspy_library()
        missing = [name for name, lib in (("libhackrf", hack), ("libairspy", air)) if lib is None]
        sdr_native.enumerate_here()    # in this process: nothing else holds libusb in a self-test
        return "libhackrf and libairspy load" if not missing else f"optional libraries missing: {', '.join(missing)}"

    def web_ui():
        from alisdr.web.server import STATIC_DIR
        missing = [f for f in ("index.html", "app.css", "icon.svg", "js/main.js") if not (STATIC_DIR / f).is_file()]
        if missing:
            raise RuntimeError(f"missing in {STATIC_DIR}: {missing}")
        return "static files ok"

    def translations():
        from alisdr import i18n
        if "pl" not in i18n.available_languages():
            raise RuntimeError("pl.po not found")
        return "pl ok"

    def band_presets():
        from alisdr import military_presets
        n = len(military_presets.get_all_presets())
        if not n:
            raise RuntimeError("no band presets")
        return f"{n} presets"

    def demodulator():
        import numpy as np
        from alisdr.dsp import Demodulator
        from alisdr.models import Modulation
        iq = (np.random.default_rng(0).standard_normal(48000) * 0.01).astype(np.complex64)
        Demodulator(2_400_000).demod(iq, Modulation.AM, 0.0)
        return "AM demod ok"

    def mp3():
        import tempfile
        import wave
        import numpy as np
        from alisdr.audio_transcode import ffmpeg_exe, transcode_to_mp3
        from alisdr.playback.audio_analysis import analyze_wav
        exe = ffmpeg_exe()
        if exe is None:
            raise RuntimeError("no ffmpeg (system or imageio-ffmpeg)")
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "t.wav"
            tone = (np.sin(np.arange(12500) * 2 * np.pi * 1000 / 12500) * 8000).astype("<i2")
            with wave.open(str(wav_path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(12500)
                w.writeframes(tone.tobytes())
            mp3_path = transcode_to_mp3(wav_path)
            if mp3_path is None:
                raise RuntimeError(f"{exe} could not encode MP3")
            decoded = analyze_wav(mp3_path)
            if not decoded.readable or decoded.duration_sec < 0.5:
                raise RuntimeError("MP3 decoded to nothing")
        return f"encode + decode ok ({Path(exe).name})"

    return [numpy_scipy, timezones, rtlsdr_library, native_receiver_libraries, web_ui, translations, band_presets, demodulator, mp3]


def run() -> int:
    failed = 0
    for check in _checks():
        try:
            print(f"OK    {check.__name__}: {check()}", flush=True)
        except Exception as exc:  # report every failure, not just the first
            failed += 1
            print(f"FAIL  {check.__name__}: {type(exc).__name__}: {exc}", flush=True)
    print("SELF-TEST " + ("PASSED" if not failed else f"FAILED ({failed})"), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
