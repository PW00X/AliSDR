"""Continuous analog listening and a welle-cli DAB/DAB+ session."""
from __future__ import annotations

import http.client
import json
import re
import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass

import numpy as np

from .agc import SoftwareAgc
from .dsp import Demodulator
from .i18n import tr
from .models import Modulation, resolve_ssb_sideband
from .scanner_engine import AUDIO_RATE
from .sdr_backend import AcquisitionThread, SoapyBackend, create_backend, device_family, parse_rtl_tcp_args

DAB_CHANNELS = tuple(f"{n}{suffix}" for n in range(5, 14) for suffix in ("ABCDEF" if n == 13 else "ABCD"))
ANALOG_MODES = ("AM", "NFM", "WFM", "SSB", "USB", "LSB")


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
            return cls(receiver, mode, channel=channel)
        frequency = body.get("frequency_hz")
        if mode not in ANALOG_MODES or type(frequency) is not int or not 1 <= frequency <= 6_000_000_000:
            raise ValueError(tr("Enter a frequency between 0 and 6000 MHz and select a modulation."))
        return cls(receiver, mode, frequency_hz=frequency)


def dab_command(device: dict, channel: str) -> list[str]:
    exe = shutil.which("welle-cli")
    if exe is None:
        raise RuntimeError(tr("Install welle-cli on the receiver computer to listen to DAB/DAB+."))
    driver = device["driver"]
    args = device.get("device_args", "")
    gain = device["gain_db"] if device["gain_mode"] == "manual" else -1
    command = [exe, "-c", channel, "-g", str(gain), "-T"]
    if device.get("bias_tee"):
        raise ValueError(tr("DAB tuning requires a receiver with Bias-T off in FalconSDR."))
    if driver == "rtl_tcp":
        host, port = parse_rtl_tcp_args(args)
        if ":" in host:
            raise ValueError(tr("welle-cli requires an IPv4 address or hostname for rtl_tcp."))
        return command + ["-F", f"rtl_tcp,{host}:{port}"]
    if (driver in ("", "auto") and not args) or (device_family(driver) == "rtl" and args in ("", "index=0")):
        return command + ["-F", "rtl_sdr"]
    if device_family(driver) == "airspy" and not args:
        return command + ["-F", "airspy"]
    if (driver in ("", "auto") or device_family(driver) == "rtl") and args.startswith("serial="):
        return command + ["-F", "soapysdr", "-s", f"driver=rtlsdr,{args}"]
    if driver not in ("", "auto", "simulated") and not driver.endswith("_native"):
        return command + ["-F", "soapysdr", "-s", ",".join(filter(None, (f"driver={driver}", args)))]
    raise ValueError(tr("welle-cli cannot select this receiver. Try its SoapySDR access method or an rtl_tcp receiver."))


def dab_services(data: dict) -> list[dict]:
    stations = []
    for service in data.get("services", []):
        sid = service.get("sid", "")
        if not re.fullmatch(r"0x[0-9a-fA-F]{1,8}", sid):
            continue
        codecs = [c.get("ascty") for c in service.get("components", []) if c.get("transportmode") == "audio"]
        codec = next((c for c in codecs if c in ("DAB", "DAB+")), None)
        # Older versions advertise playable stations with url_mp3.
        if not codec and not service.get("url_mp3"):
            continue
        label = service.get("label") or {}
        name = (label.get("fig2label") or label.get("label") or "").strip()
        stations.append({"id": int(sid, 16), "name": name or sid, "codec": codec or ""})
    return sorted(stations, key=lambda station: (station["name"].casefold(), station["id"]))


class ManualSession(threading.Thread):
    def __init__(self, tuning, device, debug, on_audio, on_end, command=None):
        super().__init__(daemon=True, name="manual-tuning")
        self.tuning = tuning
        self.device = device
        self.debug = debug
        self.on_audio = on_audio
        self.on_end = on_end
        self.command = command
        self.stop_requested = threading.Event()
        self.port = None
        self.state = "starting"

    def run(self):
        error = ""
        try:
            if self.command:
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
        try:
            rate = float(getattr(raw, "sample_rate", 0) or dev["sample_rate_hz"])
            chunk = max(4096, round(rate * 0.03 / 4096) * 4096)
            acquisition = AcquisitionThread(raw, chunk, max_queued=8, debug=self.debug)
            current = None
            while not self.stop_requested.is_set():
                tuning = self.tuning
                if tuning != current:
                    current = tuning
                    freq = tuning.frequency_hz
                    # Keep the station off the SDR's DC spike.
                    offset = min(125_000, rate / 4)
                    if freq <= offset:
                        offset = -offset
                    acquisition.set_frequency(freq - offset)
                    mode = resolve_ssb_sideband(Modulation(tuning.mode), freq)
                    demod = Demodulator(rate, AUDIO_RATE)
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
        finally:
            (acquisition if acquisition is not None else raw).close()

    def _dab(self):
        SoapyBackend._wait_for_reopen_cooldown()
        if self.stop_requested.is_set():
            return
        with socket.socket() as sock:
            sock.bind(("0.0.0.0", 0))
            self.port = sock.getsockname()[1]
        proc = subprocess.Popen(
            [*self.command, "-w", str(self.port)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            while not self.stop_requested.wait(0.25):
                if proc.poll() is not None:
                    raise RuntimeError(tr("welle-cli stopped (exit {code}). Check the receiver and decoder installation.").format(code=proc.returncode))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    def connection(self, timeout=3):
        if self.port is None or self.stop_requested.is_set():
            raise ConnectionError("DAB decoder is unavailable")
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def stations(self):
        conn = self.connection()
        try:
            conn.request("GET", "/mux.json")
            with conn.getresponse() as response:
                if response.status != 200:
                    raise ConnectionError("DAB decoder returned an error")
                return dab_services(json.loads(response.read(2 * 1024 * 1024)))
        finally:
            conn.close()
