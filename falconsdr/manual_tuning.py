"""Continuous analog listening and native DAB/DAB+ listening."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from .agc import SoftwareAgc
from .dab import AUDIO_RATE as DAB_AUDIO_RATE, CHANNELS, SAMPLE_RATE, ChannelInput, Decoder
from .dsp import Demodulator, StreamResampler, WFM_DISCRIMINATOR_RATE, level_dbfs
from .i18n import tr
from .models import Modulation, resolve_ssb_sideband
from .rds import RDSDecoder
from .scanner_engine import AUDIO_RATE, SpectrumWorker
from .sdr_backend import AcquisitionThread, create_backend, open_real_backend

DAB_CHANNELS = tuple(CHANNELS)
DAB_DISCOVERY_TIMEOUT = 20
DAB_GAIN_SETTLE_S = 5
ANALOG_MODES = ("AM", "NFM", "WFM", "SSB", "USB", "LSB")
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tuning:
    receiver: int
    mode: str
    frequency_hz: int = 0
    channel: str = ""

    @classmethod
    def from_request(cls, body: dict) -> Tuning:
        if not isinstance(body, dict):
            raise ValueError("Invalid tuning request")
        receiver = body.get("receiver", 0)
        if type(receiver) is not int:
            raise ValueError(tr("Invalid receiver."))
        mode = body.get("mode")
        if mode == "DAB":
            channel = body.get("channel")
            if channel not in DAB_CHANNELS:
                raise ValueError(tr("Choose a DAB channel."))
            return cls(receiver, mode, frequency_hz=CHANNELS[channel], channel=channel)
        frequency = body.get("frequency_hz")
        if mode not in ANALOG_MODES or type(frequency) is not int or not 1 <= frequency <= 6_000_000_000:
            raise ValueError(tr("Enter a frequency between 0 and 6000 MHz and select a modulation."))
        return cls(receiver, mode, frequency_hz=frequency)


class ManualSession(threading.Thread):
    def __init__(self, tuning, device, debug, on_audio, on_end, dab_library=None):
        super().__init__(daemon=True, name="manual-tuning")
        self.tuning = tuning
        self.device = device
        self.debug = debug
        self.on_audio = on_audio
        self.on_end = on_end
        self.dab_library = dab_library
        self.stop_requested = threading.Event()
        self.stations = {}
        self.station = 0
        self.pending_station = 0
        self.station_info = {}
        self.on_dab_audio = lambda _pcm: None
        self.dab_error = ""
        self.synced = False
        self.state = "starting"
        self.spectrum = None
        self.level = None
        self.gain_range = None

    def _update_spectrum(self, freqs, db, _trigger, _custom):
        self.spectrum = {"f0": float(freqs[0]), "f1": float(freqs[-1]), "db": np.round(db, 1).tolist()}

    def run(self):
        error = ""
        try:
            if self.tuning.mode == "DAB":
                self._dab()
            else:
                self._analog()
        except Exception as exc:
            if not self.stop_requested.is_set():
                error = str(exc)
        finally:
            self.on_end(self, error)

    def _analog(self):
        dev = self.device
        gain_db = dev["gain_db"]
        software_gain = SoftwareAgc(gain_db) if dev["gain_mode"] == "sw_auto" else None
        raw = create_backend(
            dev["driver"], dev.get("device_args", ""), dev["sample_rate_hz"],
            "manual" if software_gain else dev["gain_mode"], gain_db,
            debug_mode=self.debug, bias_tee=bool(dev.get("bias_tee")), async_read=dev["async_read"],
        )
        acquisition = None
        spectrum = SpectrumWorker(self._update_spectrum)
        try:
            rate = float(getattr(raw, "sample_rate", 0) or dev["sample_rate_hz"])
            chunk = max(4096, round(rate * 0.03 / 4096) * 4096)
            acquisition = AcquisitionThread(raw, chunk, max_queued=8, debug=self.debug)
            self.gain_range = acquisition.gain_range_db
            current = None
            next_frame = 0
            while not self.stop_requested.is_set():
                tuning = self.tuning
                if tuning != current:
                    current = tuning
                    freq = tuning.frequency_hz
                    # Keep the station off the SDR's DC spike.
                    room = max(0, (rate - WFM_DISCRIMINATOR_RATE) / 2) if tuning.mode == "WFM" else rate / 4
                    offset = min(125_000, rate / 4, room)
                    if freq <= offset:
                        offset = -offset
                    acquisition.set_frequency(freq - offset)
                    mode = resolve_ssb_sideband(Modulation(tuning.mode), freq)
                    demod = Demodulator(rate, AUDIO_RATE)
                    self.station_info = {}
                    if mode == Modulation.WFM:
                        rds = RDSDecoder()
                        demod.on_fm_composite = rds.feed
                        self.station_info = rds.info
                    acquisition.read(chunk)  # discard the first block while the tuner settles
                iq = acquisition.read(chunk)
                if software_gain and iq.size:
                    new_gain = software_gain.step(float(np.max(np.abs(iq))), acquisition.gain_range_db)
                    if new_gain is not None:
                        acquisition.set_gain("manual", new_gain)
                        demod.notify_rf_gain_change(10 ** ((new_gain - gain_db) / 20))
                        gain_db = new_gain
                if self.tuning != current or self.stop_requested.is_set():
                    continue
                self.state = "listening"
                self.on_audio(demod.demod(iq, mode, offset))
                now = time.monotonic()
                if now >= next_frame:
                    next_frame = now + 0.35
                    self.level = level_dbfs(iq)
                    spectrum.submit(iq, rate, freq - offset, 0)
        finally:
            spectrum.stop()
            (acquisition if acquisition is not None else raw).close()

    def select_station(self, sid):
        if sid not in self.stations or self.stop_requested.is_set():
            raise ValueError(tr("DAB station is unavailable."))
        self.station = sid
        self.pending_station = 0
        self.station_info.pop("text", None)

    def _dab_station(self, sid, name, codec):
        label = name.decode("utf-8", errors="replace").strip()
        self.stations[sid] = {"id": sid, "name": label or f"{sid:04X}", "codec": "DAB+" if codec == 63 else "DAB"}

    def _dab_reception(self, synced, _snr, _offset):
        self.synced = synced

    def _dab_metadata(self, sid, label):
        if label and (sid == 0 or sid == self.station):
            self.station_info["ensemble" if sid == 0 else "text"] = label.decode("utf-8", errors="replace").strip()

    def _dab(self):
        dev = self.device
        gain = dev["gain_db"]
        software_gain = dev["gain_mode"] == "sw_auto"
        raw = open_real_backend(
            dev["driver"], dev.get("device_args", ""), SAMPLE_RATE,
            "manual" if software_gain else dev["gain_mode"], gain,
            bias_tee=bool(dev.get("bias_tee")), async_read=dev["async_read"],
        )
        acquisition = decoder = None
        spectrum = SpectrumWorker(self._update_spectrum)
        try:
            rate = float(getattr(raw, "sample_rate", 0) or SAMPLE_RATE)
            if rate < 1_536_000:
                raise RuntimeError(tr("The receiver capture bandwidth is too narrow for DAB/DAB+."))
            chunk = max(4096, round(rate * .03 / 4096) * 4096)
            acquisition = AcquisitionThread(raw, chunk, max_queued=12, debug=self.debug)
            self.gain_range = acquisition.gain_range_db
            frequency = CHANNELS[self.tuning.channel]
            acquisition.set_frequency(frequency)
            acquisition.read(chunk)
            if dev["gain_mode"] != "auto":
                lo, hi = acquisition.gain_range_db
                gain = max(lo, min(hi, gain))
            if software_gain:
                # Gain steps flush the Airspy's IQ queues. Let software AGC
                # settle first, then give the decoder an uninterrupted stream.
                agc = SoftwareAgc(gain)
                deadline = time.monotonic() + DAB_GAIN_SETTLE_S
                while not self.stop_requested.is_set() and time.monotonic() < deadline:
                    iq = acquisition.read(chunk)
                    new_gain = agc.step(float(np.max(np.abs(iq))), acquisition.gain_range_db)
                    if new_gain is not None:
                        acquisition.set_gain("manual", new_gain)
                        gain = new_gain
            if self.stop_requested.is_set():
                return
            channel = ChannelInput(rate)
            stream_stats = getattr(raw, "stream_stats", None)
            lost_before = stream_stats()[0] if stream_stats else 0.0
            dropped_before = acquisition.chunks_dropped
            decoder = Decoder(self.dab_library, self._dab_station, self._dab_reception, self._dab_metadata)
            self.state = "discovering"
            started = time.monotonic()
            next_meter = started
            previous_sid = previous_rate = 0
            last_audio = started
            audio_resampler = None
            while not self.stop_requested.is_set():
                iq = acquisition.read(chunk)
                now = time.monotonic()
                conditioned = channel.process(iq)
                if now >= next_meter:
                    self.level = level_dbfs(conditioned)
                    spectrum.submit(iq, rate, frequency, 0)
                    next_meter = now + 0.35
                decoder.push(conditioned)
                if self.stations:
                    self.state = "listening"
                elif now - started >= DAB_DISCOVERY_TIMEOUT:
                    lost = stream_stats()[0] - lost_before if stream_stats else 0.0
                    _log.warning(
                        "DAB %s %d Hz via %s: %.3f MS/s, gain %s (configured %.1f, last %.1f), "
                        "%.1f dBFS channel level, %d dropped acquisition blocks, %.3f s native IQ lost",
                        self.tuning.channel, frequency, raw.label, rate / 1e6, dev["gain_mode"],
                        dev["gain_db"], gain, self.level, acquisition.chunks_dropped - dropped_before, lost,
                    )
                    message = ("DAB channel {channel} synchronized, but no audio stations were decoded."
                               if self.synced else "No DAB signal could be synchronized on channel {channel}.")
                    raise RuntimeError(tr(message).format(channel=self.tuning.channel))

                if self.pending_station in self.stations:
                    self.select_station(self.pending_station)
                sid = self.station
                if sid != previous_sid:
                    decoder.select(sid)
                    previous_sid, previous_rate = sid, 0
                    last_audio = now
                    self.dab_error = ""
                for audio_rate, pcm in decoder.drain_audio():
                    if sid != self.station or self.stop_requested.is_set():
                        break
                    if audio_rate != previous_rate:
                        audio_resampler = StreamResampler(audio_rate, AUDIO_RATE)
                        stereo_resamplers = None if audio_rate == DAB_AUDIO_RATE else [
                            StreamResampler(audio_rate, DAB_AUDIO_RATE) for _ in range(2)
                        ]
                        previous_rate = audio_rate
                    stereo = pcm.reshape(-1, 2)
                    if stereo_resamplers:
                        channels = [resampler.process(stereo[:, i].astype(np.float32)).real
                                    for i, resampler in enumerate(stereo_resamplers)]
                        stereo_out = np.clip(np.column_stack(channels), -32768, 32767).astype("<i2")
                    else:
                        stereo_out = stereo.astype("<i2", copy=False)
                    self.on_dab_audio(stereo_out.tobytes())
                    mono = stereo.mean(axis=1, dtype=np.float32) / 32768.0
                    self.on_audio(audio_resampler.process(mono).real)
                    last_audio = now
                    self.dab_error = ""
                if sid and now - last_audio >= 12:
                    self.dab_error = tr("The DAB station is selected, but no audio is being decoded.")
        finally:
            spectrum.stop()
            if decoder is not None:
                decoder.close()
            (acquisition if acquisition is not None else raw).close()
