"""Decode a scanner slot's shared IQ without opening the receiver a second time."""
import queue
import subprocess
import threading

import numpy as np

from ..dsp import Demodulator, StreamResampler
from .external import parse_vdl2


class ScanVdl2:
    def __init__(self, program, frequency, offset, rate, publish, log):
        self.frequency = frequency
        self.publish = publish
        self.log = log
        self.command = [program, "--iq-file", "-", "--sample-format", "S16_LE",
                        "--oversample", "2", "--centerfreq", str(frequency),
                        "--output", "decoded:json:file:path=-", str(frequency)]
        self.rate, self.offset = rate, offset
        self.blocks = queue.Queue(maxsize=8)
        self.sequence = 0
        self.ended = threading.Event()
        self.decoded = False
        self.process = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="scan-vdl2")
        self.thread.start()

    def feed(self, iq):
        if self.ended.is_set():
            return
        self.sequence += 1
        try:
            self.blocks.put_nowait((self.sequence, iq))
        except queue.Full:
            pass  # Never block the scanner. The next accepted block signals the gap.

    def end_input(self):
        self.ended.set()

    def _messages(self, stream):
        with stream:
            for line in stream:
                parsed = parse_vdl2(line.decode("utf-8", errors="replace"))
                if parsed is None:
                    continue
                event, summary = parsed
                if event["freq_hz"] == self.frequency and (event["src"] or event["dst"]):
                    self.decoded = True
                    self.publish(event, summary)

    def _run(self):
        reader = None
        try:
            mixer = Demodulator(self.rate, 210000)
            resampler = StreamResampler(self.rate, 210000)
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL)
            reader = threading.Thread(target=self._messages, args=(self.process.stdout,), daemon=True)
            reader.start()
            with self.process.stdin as sink:
                previous = 0
                while not self.ended.is_set() or not self.blocks.empty():
                    try:
                        sequence, block = self.blocks.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if previous and sequence != previous + 1:
                        sink.write(bytes(8192))
                        resampler = StreamResampler(self.rate, 210000)
                    previous = sequence
                    iq = resampler.process(mixer._mix(block, self.offset))
                    samples = np.ascontiguousarray(iq, dtype=np.complex64).view(np.float32)
                    pcm = np.clip(samples * 32768, -32768, 32767).astype("<i2")
                    sink.write(pcm.tobytes())
                # Flush the modem's final symbol/filter tail before EOF.
                sink.write(bytes(8192))
            self.process.wait(timeout=3)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            self.log(f"dumpvdl2: {exc}")
        finally:
            self.end_input()
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            if reader is not None:
                reader.join()

    def finish(self):
        self.end_input()
        self.thread.join(timeout=4)
        if self.thread.is_alive() and self.process is not None and self.process.poll() is None:
            self.process.kill()
        self.thread.join()
        return self.decoded
