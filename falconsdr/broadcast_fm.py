"""Decode the FM multiplex into 48 kHz audio for Manual listening."""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, lfilter, sosfilt

from .dsp import StreamResampler, WFM_DEEMPHASIS_TAU_S

AUDIO_RATE = 48_000


class BroadcastFM:
    def __init__(self, rate: int):
        self.rate = rate
        self._samples = 0
        self._blend = 0.0
        self._pilot_filter = butter(2, 100, fs=rate, output="sos")
        self._pilot_state = np.zeros((len(self._pilot_filter), 2), dtype=np.complex128)
        self._lowpass = butter(8, 15_000, fs=rate, output="sos")
        self._lowpass_state = np.zeros((len(self._lowpass), 2), dtype=np.complex128)
        self._resampler = StreamResampler(rate, AUDIO_RATE)
        self._dc_filter = butter(1, 25, btype="highpass", fs=AUDIO_RATE, output="sos")
        self._dc_state = np.zeros((len(self._dc_filter), 2), dtype=np.complex128)
        self._deemphasis = 1 - np.exp(-1 / (AUDIO_RATE * WFM_DEEMPHASIS_TAU_S))
        self._deemphasis_state = np.zeros(1, dtype=np.complex128)
        self._oscillator = None

    def process(self, composite: np.ndarray) -> np.ndarray:
        size = composite.size
        if not size:
            return np.empty((0, 2), dtype=np.float32)
        multiplex = composite * (self.rate / (2 * np.pi * 75_000))
        difference = 0
        if self.rate > 110_000:
            if self._oscillator is None or self._oscillator.size != size:
                self._oscillator = np.exp(np.arange(size) * (2j * np.pi * 19_000 / self.rate))
            oscillator = self._oscillator * np.exp(self._samples * (2j * np.pi * 19_000 / self.rate))
            self._samples = (self._samples + size) % self.rate
            pilot, self._pilot_state = sosfilt(self._pilot_filter, multiplex * oscillator.conj(),
                                               zi=self._pilot_state)
            strength = np.abs(pilot)
            # The pilot is nominally 9% of full modulation, halved by mixing.
            # Blend towards mono as the pilot fades, avoiding stereo noise bursts.
            target = float(np.clip((np.median(strength) - 0.015) / 0.018, 0, 1))
            next_blend = target + (self._blend - target) * np.exp(-size / (self.rate * 0.15))
            blend = np.linspace(self._blend, next_blend, size)
            self._blend = next_blend
            phase = (pilot * oscillator) / np.maximum(strength, 1e-9)
            carrier = -(phase * phase).imag
            difference = 2 * multiplex * carrier * blend
        # Real and imaginary lanes carry L+R and L-R through identical filters
        # and the same clock, so there is no relative delay at recombination.
        packed, self._lowpass_state = sosfilt(self._lowpass, multiplex + 1j * difference,
                                               zi=self._lowpass_state)
        packed = self._resampler.process(packed)
        if not packed.size:
            return np.empty((0, 2), dtype=np.float32)
        packed, self._dc_state = sosfilt(self._dc_filter, packed, zi=self._dc_state)
        alpha = self._deemphasis
        packed, self._deemphasis_state = lfilter([alpha], [1.0, alpha - 1.0], packed,
                                                 zi=self._deemphasis_state)
        return np.column_stack((packed.real + packed.imag, packed.real - packed.imag))
