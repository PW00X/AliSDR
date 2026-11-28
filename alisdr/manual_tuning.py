"""Continuous analog listening and native DAB/DAB+ listening."""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

import numpy as np

from .agc import SoftwareAgc
from .broadcast_fm import AUDIO_RATE as FM_AUDIO_RATE, BroadcastFM
from .channelizer import channel_powers_from_spectrum, plan_windows, spectrum_cumsum
from .dab import AUDIO_RATE as DAB_AUDIO_RATE, CHANNELS, SAMPLE_RATE, ChannelInput, Decoder
from .dsp import Demodulator, StreamResampler, WFM_DISCRIMINATOR_RATE, level_dbfs
from .i18n import tr
from .models import Modulation, resolve_ssb_sideband
from .rds import RDSDecoder
from .music import track_label
from .scanner_engine import AUDIO_RATE, SpectrumWorker
from .sdr_backend import AcquisitionThread, create_backend, open_real_backend

DAB_CHANNELS = tuple(CHANNELS)
FM_FREQUENCIES = tuple(range(87_500_000, 108_000_001, 100_000))
FM_SCAN_WIDTH_HZ = 90_000
FM_SCAN_SNR_DB = 10
FM_RDS_DWELL_S = 4
TRACK_CONFIRM_S = 5
DAB_DISCOVERY_TIMEOUT = 20
DAB_RETRY_AFTER_S = 8
DAB_MONITOR_DWELL_S = 10  # Time to acquire a label before trying the next service.
DAB_TUNER_SETTLE_S = 0.35
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
        self.tune_id = uuid4().hex
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
        self.station_image = None
        self.on_music = None
        self.monitor_enabled = lambda: False
        self._last_tracks = {}
        self._pending_tracks = {}
        self._monitor_track_station = None
        self._music_lock = threading.Lock()
        self.on_broadcast_audio = lambda _pcm: None
        self.fm_scan = None
        self.fm_stations = None
        self.dab_error = ""
        self.synced = False
        self.state = "starting"
        self.spectrum = None
        self.level = None
        self.gain_range = None

    def request_gain(self, mode, db):
        # Publish the pair together. Only the tuning thread sends hardware commands.
        if (mode, db) != (self.device["gain_mode"], self.device["gain_db"]):
            self.device = {**self.device, "gain_mode": mode, "gain_db": db}

    @staticmethod
    def _apply_live_gain(acquisition, device):
        mode = device["gain_mode"]
        lo, hi = acquisition.gain_range_db
        gain = max(lo, min(hi, device["gain_db"]))
        software = SoftwareAgc(gain) if mode == "sw_auto" else None
        acquisition.set_gain("manual" if software else mode, gain)
        return gain, software

    def _music_text(self, station_id, station, text, *, monitored=False):
        track = track_label(text)
        key = track.casefold()
        with self._music_lock:
            if monitored:
                self._monitor_track_station = station_id if track else None
            if not track or self._last_tracks.get(station_id) == key:
                self._pending_tracks.pop(station_id, None)
                return
            pending = self._pending_tracks.get(station_id)
            if not pending or pending[1].casefold() != key:
                self._pending_tracks[station_id] = (station, track, time.monotonic() + TRACK_CONFIRM_S)

    def _confirm_music(self, listening):
        # Decoders can report a label only when it changes. Advance its timer
        # from the receive loop while the station is still being decoded.
        with self._music_lock:
            now = time.monotonic()
            for station_id, (station, track, deadline) in list(self._pending_tracks.items()):
                if station_id not in listening or self.stop_requested.is_set():
                    del self._pending_tracks[station_id]
                elif now >= deadline:
                    del self._pending_tracks[station_id]
                    self._last_tracks[station_id] = track.casefold()
                    if self.on_music:
                        self.on_music(station_id, station, track, time.time())

    def _update_spectrum(self, freqs, db, _trigger, _custom):
        self.spectrum = {"f0": float(freqs[0]), "f1": float(freqs[-1]), "db": np.round(db, 1).tolist()}

    def run(self):
        error = ""
        try:
            if self.fm_scan is not None:
                self._scan_fm()
            elif self.tuning.mode == "DAB":
                self._dab()
            else:
                self._analog()
        except Exception as exc:
            if not self.stop_requested.is_set():
                error = str(exc)
        finally:
            self.on_end(self, error)

    def _scan_fm(self) -> None:
        dev = self.device
        raw = create_backend(
            dev["driver"], dev.get("device_args", ""), dev["sample_rate_hz"],
            "manual" if dev["gain_mode"] == "sw_auto" else dev["gain_mode"], dev["gain_db"],
            debug_mode=self.debug, bias_tee=bool(dev.get("bias_tee")), async_read=dev["async_read"],
        )
        acquisition = None
        try:
            rate = float(getattr(raw, "sample_rate", 0) or dev["sample_rate_hz"])
            if rate < 200_000:
                raise RuntimeError(tr("The receiver capture bandwidth is too narrow for FM scanning."))
            chunk = max(4096, round(rate * 0.03 / 4096) * 4096)
            acquisition = AcquisitionThread(raw, chunk, max_queued=8, debug=self.debug)
            self.gain_range = acquisition.gain_range_db
            self.state = "fm_scanning"
            agc = SoftwareAgc(dev["gain_db"]) if dev["gain_mode"] == "sw_auto" else None
            candidates = self._survey_fm(acquisition, chunk, rate, agc)
            for index, hz in enumerate(candidates, 1):
                if self.stop_requested.is_set():
                    break
                self.fm_scan = {"stage": "rds", "done": index, "total": len(candidates), "frequency_hz": hz}
                station = self._identify_fm(acquisition, chunk, rate, hz)
                if not self.stop_requested.is_set():
                    self.fm_stations[hz] = {"frequency_hz": hz, "name": station.get("name", ""), "pi": station.get("pi", "")}
        finally:
            (acquisition if acquisition is not None else raw).close()

    def _survey_fm(self, acquisition, chunk: int, rate: float, agc: SoftwareAgc | None = None) -> list[int]:
        windows = plan_windows(list(FM_FREQUENCIES), rate, FM_SCAN_WIDTH_HZ)
        levels = {}
        above_noise = set()
        for index, window in enumerate(windows, 1):
            if self.stop_requested.is_set():
                return []
            self.fm_scan = {"stage": "sweep", "done": index, "total": len(windows),
                            "frequency_hz": window.center_hz}
            acquisition.set_frequency(window.center_hz)
            settled = acquisition.read(chunk)
            if agc and settled.size:
                new_gain = agc.step(float(np.max(np.abs(settled))), acquisition.gain_range_db)
                if new_gain is not None:
                    acquisition.set_gain("manual", new_gain)
                    acquisition.read(chunk)
            readings = []
            references = []
            for _ in range(3):
                iq = acquisition.read(chunk)
                spectrum = spectrum_cumsum(iq - iq.mean(), rate) if iq.size else None
                if spectrum is None:
                    continue
                bins, cumulative = spectrum
                # Estimate in-channel noise from the lower half of the capture
                # so that a strong station does not raise its own threshold.
                noise = np.percentile(np.diff(cumulative), 35) * FM_SCAN_WIDTH_HZ / (bins[1] - bins[0])
                references.append(10 * np.log10(max(noise, 1e-12)))
                readings.append(channel_powers_from_spectrum(spectrum, window.center_hz, window.freqs, FM_SCAN_WIDTH_HZ))
            if not readings:
                continue
            threshold = float(np.median(references)) + FM_SCAN_SNR_DB
            for hz in window.freqs:
                levels[hz] = float(np.median([row[hz] for row in readings]))
                if levels[hz] >= threshold:
                    above_noise.add(hz)
        # The same wide signal often spills into adjacent slots on the 100 kHz
        # raster. Keep the local peak so a station is not listed twice.
        return [hz for hz in sorted(above_noise)
                if levels[hz] >= levels.get(hz - 100_000, -math.inf)
                and levels[hz] > levels.get(hz + 100_000, -math.inf)]

    def _identify_fm(self, acquisition, chunk: int, rate: float, hz: int) -> dict:
        offset = min(125_000, (rate - WFM_DISCRIMINATOR_RATE) / 2)
        acquisition.set_frequency(hz - offset)
        acquisition.read(chunk)
        demod = Demodulator(rate)
        rds = RDSDecoder()
        for _ in range(math.ceil(FM_RDS_DWELL_S * rate / chunk)):
            if self.stop_requested.is_set():
                break
            iq = acquisition.read(chunk)
            if not iq.size:
                continue
            composite, fm_rate = demod.fm_composite(iq, offset)
            rds.feed(composite, fm_rate)
            if rds.info.get("name"):
                break
        return rds.info

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
                requested = self.device
                if requested is not dev:
                    previous_gain = gain_db
                    dev = requested
                    gain_db, software_gain = self._apply_live_gain(acquisition, dev)
                    if current is not None and dev["gain_mode"] != "auto":
                        demod.notify_rf_gain_change(10 ** ((gain_db - previous_gain) / 20))
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
                    self._last_tracks.clear()
                    self._pending_tracks.clear()
                    previous_text = None
                    fm_audio = None
                    if mode == Modulation.WFM:
                        rds = RDSDecoder()
                        fm_audio = BroadcastFM(min(WFM_DISCRIMINATOR_RATE, int(round(rate))))
                        fm_mono = StreamResampler(FM_AUDIO_RATE, AUDIO_RATE)
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
                if fm_audio is not None and iq.size:
                    composite, fm_rate = demod.fm_composite(iq, offset)
                    if fm_rate > 120_000:
                        rds.feed(composite, fm_rate)
                        text = rds.info.get("text")
                        station_id = f"FM:{freq}"
                        if text != previous_text:
                            previous_text = text
                            self._music_text(station_id, rds.info.get("name") or f"{freq / 1e6:g} MHz", text or "")
                        self._confirm_music((station_id,))
                    stereo = fm_audio.process(composite)
                    if stereo.size:
                        self.on_broadcast_audio((np.clip(stereo, -1.0, 1.0) * 32767).astype("<i2").tobytes())
                        self.on_audio(fm_mono.process(stereo.mean(axis=1)).real)
                else:
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
        self.pending_station = 0
        if self.station != sid:
            with self._music_lock:
                self._pending_tracks.pop(f"DAB:{self.tuning.channel}:{self.station}", None)
            self.station = sid
            self.station_info.pop("text", None)
            self.station_image = None

    def _dab_station(self, sid, name, codec):
        label = name.decode("utf-8", errors="replace").strip()
        self.stations[sid] = {"id": sid, "name": label or f"{sid:04X}", "codec": "DAB+" if codec == 63 else "DAB"}

    def _dab_reception(self, synced, _snr, _offset):
        self.synced = synced
        if not synced:
            with self._music_lock:
                self._pending_tracks.clear()
                self._monitor_track_station = None

    def _dab_metadata(self, sid, label):
        if sid and sid != self.station:
            self._monitor_label(sid, label)
            return
        if label is not None:
            text = label.decode("utf-8", errors="replace").strip()
            self.station_info["ensemble" if sid == 0 else "text"] = text
            if sid:
                self._music_text(f"DAB:{self.tuning.channel}:{sid}", self.stations[sid]["name"], text)

    def _monitor_label(self, sid, label):
        station = self.stations.get(sid)
        if station and sid != self.station and label is not None and self.monitor_enabled():
            self._music_text(f"DAB:{self.tuning.channel}:{sid}", station["name"],
                             label.decode("utf-8", errors="replace").strip(), monitored=True)

    def _dab_image(self, sid, data):
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            media_type = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            media_type = "image/jpeg"
        else:
            return
        if sid == self.station:
            self.station_image = (sid, hashlib.sha256(data).hexdigest(), media_type, data)

    def _settle_dab(self, acquisition, chunk, frequency, gain, software_gain):
        acquisition.set_frequency(frequency)
        # The first few blocks can include the previous capture or startup AGC.
        # Discard them before initializing the OFDM decoder.
        agc = SoftwareAgc(gain) if software_gain else None
        deadline = time.monotonic() + (DAB_GAIN_SETTLE_S if agc else DAB_TUNER_SETTLE_S)
        while not self.stop_requested.is_set() and time.monotonic() < deadline:
            iq = acquisition.read(chunk)
            if agc and iq.size:
                new_gain = agc.step(float(np.max(np.abs(iq))), acquisition.gain_range_db)
                if new_gain is not None:
                    acquisition.set_gain("manual", new_gain)
                    gain = new_gain
        return gain

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
        monitor_sid = 0
        next_monitor = 0.0
        monitor_failed = False
        spectrum = SpectrumWorker(self._update_spectrum)
        try:
            rate = float(getattr(raw, "sample_rate", 0) or SAMPLE_RATE)
            if rate < 1_536_000:
                raise RuntimeError(tr("The receiver capture bandwidth is too narrow for DAB/DAB+."))
            chunk = max(4096, round(rate * .03 / 4096) * 4096)
            acquisition = AcquisitionThread(raw, chunk, max_queued=12, debug=self.debug)
            self.gain_range = acquisition.gain_range_db
            frequency = CHANNELS[self.tuning.channel]
            lo, hi = acquisition.gain_range_db
            if dev["gain_mode"] != "auto":
                gain = max(lo, min(hi, gain))
            gain = self._settle_dab(acquisition, chunk, frequency, gain, software_gain)
            if self.stop_requested.is_set():
                return
            channel = ChannelInput(rate)
            stream_stats = getattr(raw, "stream_stats", None)
            lost_before = stream_stats()[0] if stream_stats else 0.0
            dropped_before = acquisition.chunks_dropped
            decoder = Decoder(self.dab_library, self._dab_station, self._dab_reception,
                              self._dab_metadata, self._dab_image)
            self.state = "discovering"
            started = time.monotonic()
            next_meter = started
            retried = False
            ever_synced = False
            live_agc = SoftwareAgc(gain) if software_gain else None
            previous_sid = previous_rate = 0
            last_audio = started
            audio_resampler = None
            while not self.stop_requested.is_set():
                iq = acquisition.read(chunk)
                now = time.monotonic()
                requested = self.device
                if requested is not dev:
                    dev = requested
                    gain, live_agc = self._apply_live_gain(acquisition, dev)
                elif live_agc and iq.size:
                    step = live_agc.step(float(np.max(np.abs(iq))), acquisition.gain_range_db)
                    if step is not None:
                        gain = step
                        acquisition.set_gain("manual", gain)
                conditioned = channel.process(iq)
                if now >= next_meter:
                    self.level = level_dbfs(conditioned)
                    spectrum.submit(iq, rate, frequency, 0)
                    next_meter = now + 0.35
                decoder.push(conditioned)
                enabled = self.monitor_enabled()
                listening = {f"DAB:{self.tuning.channel}:{self.station}"}
                if enabled and monitor_sid and not monitor_failed:
                    listening.add(f"DAB:{self.tuning.channel}:{monitor_sid}")
                self._confirm_music(listening)
                if enabled and not monitor_failed and len(self.stations) > 1:
                    monitor_key = f"DAB:{self.tuning.channel}:{monitor_sid}"
                    with self._music_lock:
                        pending = monitor_key in self._pending_tracks
                        ready = self._monitor_track_station == monitor_key and not pending
                    # A new title gets its confirmation time even late in the visit.
                    # Bound the extension so unstable text cannot stall the rotation.
                    expired = now >= next_monitor and (not pending or now >= next_monitor + TRACK_CONFIRM_S)
                    if expired or ready or monitor_sid == self.station:
                        choices = sorted(self.stations.keys() - {self.station})
                        monitor_sid = next((sid for sid in choices if sid > monitor_sid), choices[0])
                        with self._music_lock:
                            self._monitor_track_station = None
                        monitor_failed = not decoder.monitor(monitor_sid)
                        if monitor_failed:
                            _log.warning("Update the native DAB library to enable shared music monitoring")
                        next_monitor = now + DAB_MONITOR_DWELL_S
                elif not enabled and monitor_sid:
                    decoder.monitor(0)
                    with self._music_lock:
                        self._monitor_track_station = None
                    monitor_sid = 0
                    next_monitor = 0.0
                    monitor_failed = False
                ever_synced |= self.synced
                if self.stations:
                    self.state = "listening"
                elif not retried and DAB_RETRY_AFTER_S < DAB_DISCOVERY_TIMEOUT and now - started >= DAB_RETRY_AFTER_S:
                    # OFDM sync alone does not establish that the ensemble's
                    # service information can be decoded. Restart discovery
                    # after another hardware tune, without reopening USB.
                    retried = True
                    decoder.close()
                    decoder = None
                    self.station_info.clear()
                    self.synced = False
                    fallback_gain = dev["gain_mode"] == "auto"
                    if fallback_gain:
                        gain = (lo + hi) / 2
                        acquisition.set_gain("manual", gain)
                    gain = self._settle_dab(acquisition, chunk, frequency, gain, fallback_gain)
                    if self.stop_requested.is_set():
                        return
                    channel = ChannelInput(rate)
                    decoder = Decoder(self.dab_library, self._dab_station, self._dab_reception,
                                      self._dab_metadata, self._dab_image)
                    started = time.monotonic()
                    continue
                elif now - started >= DAB_DISCOVERY_TIMEOUT:
                    lost = stream_stats()[0] - lost_before if stream_stats else 0.0
                    _log.warning(
                        "DAB %s %d Hz via %s: %.3f MS/s, gain %s (configured %.1f, last %.1f), "
                        "%.1f dBFS channel level, %d dropped acquisition blocks, %.3f s native IQ lost",
                        self.tuning.channel, frequency, raw.label, rate / 1e6, dev["gain_mode"],
                        dev["gain_db"], gain, self.level, acquisition.chunks_dropped - dropped_before, lost,
                    )
                    message = ("DAB channel {channel} synchronized, but no audio stations were decoded."
                               if ever_synced else "No DAB signal could be synchronized on channel {channel}.")
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
                    self.on_broadcast_audio(stereo_out.tobytes())
                    mono = stereo.mean(axis=1, dtype=np.float32) / 32768.0
                    self.on_audio(audio_resampler.process(mono).real)
                    last_audio = now
                    self.dab_error = ""
                if sid and now - last_audio > 0.35:
                    self.on_broadcast_audio(b"")
                if sid and now - last_audio >= 12:
                    self.dab_error = tr("The DAB station is selected, but no audio is being decoded.")
        finally:
            spectrum.stop()
            if decoder is not None:
                decoder.close()
            (acquisition if acquisition is not None else raw).close()
