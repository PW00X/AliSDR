"""Feed a selected USB Airspy to dumpvdl2 at exactly 20 x 105 kS/s."""
import signal
import subprocess
import sys

import numpy as np

from ..dsp import StreamResampler
from ..sdr_backend import AcquisitionThread, open_real_backend


def run(driver, args, center, gain, ppm, bias, command):
    receiver = open_real_backend(driver, args, 2_500_000, "manual" if gain else "auto",
                                 float(gain or 0), bias_tee=bias)
    chunk = 65536
    capture = None
    decoder = None
    try:
        converter = StreamResampler(receiver.sample_rate, 2_100_000)
        capture = AcquisitionThread(receiver, chunk)
        capture.set_frequency(round(center * (1 + ppm / 1_000_000)))
        decoder = subprocess.Popen(command, stdin=subprocess.PIPE)
        while decoder.poll() is None:
            iq = converter.process(capture.read(chunk))
            samples = np.ascontiguousarray(iq, dtype=np.complex64).view(np.float32)
            pcm = np.clip(samples * 32768, -32768, 32767).astype("<i2")
            decoder.stdin.write(pcm.tobytes())
        return decoder.returncode
    except BrokenPipeError:
        return decoder.wait(timeout=2) or 1
    finally:
        try:
            if decoder is not None:
                if decoder.poll() is None:
                    decoder.terminate()
                try:
                    decoder.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    decoder.kill()
                    decoder.wait()
                try:
                    decoder.stdin.close()
                except BrokenPipeError:
                    pass
        finally:
            (capture or receiver).close()


def main():
    driver, args, center, gain, ppm, bias, *command = sys.argv[1:]

    def stop(_signal, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    return run(driver, args, int(center), gain, float(ppm or 0), bias == "1", command)


if __name__ == "__main__":
    raise SystemExit(main())
