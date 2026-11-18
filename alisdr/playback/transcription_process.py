"""Keep Whisper cancellable, including native model loading."""
from __future__ import annotations

import multiprocessing
import threading

from .transcription_engine import ModelUnavailable, transcribe_recording


class TranscriptionPaused(Exception):
    """The recording should go back on the queue for a later attempt."""


def _serve(connection) -> None:
    with connection:
        while True:
            try:
                recording, config, language = connection.recv()
            except EOFError:
                return
            try:
                result = transcribe_recording(recording, config, forced_language=language)
            except ModelUnavailable as exc:
                connection.send((None, str(exc)))
            else:
                connection.send((result, None))


class TranscriptionProcess:
    """Reuse the child and its models until the receiver needs its resources."""

    def __init__(self, paused) -> None:
        self._paused = paused
        self._lock = threading.Lock()
        self._process = None
        self._connection = None

    def transcribe(self, recording, config, language):
        with self._lock:
            if self._paused():
                raise TranscriptionPaused
            if self._process is None:
                self._start()
            try:
                self._connection.send((recording, config, language))
                while not self._paused():
                    if self._connection.poll(0.1):
                        result, error = self._connection.recv()
                        if error is not None:
                            raise ModelUnavailable(error)
                        return result
            except (EOFError, OSError) as exc:
                self._close()
                if self._paused():
                    raise TranscriptionPaused from exc
                raise ModelUnavailable("Transcription process exited") from exc
            self._close()
            raise TranscriptionPaused

    def _start(self) -> None:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=_serve, args=(child,), daemon=True, name="alisdr-transcription")
        try:
            process.start()
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self._connection, self._process = parent, process

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self) -> None:
        if self._process is None:
            return
        self._connection.close()
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=0.5)
        if self._process.is_alive():
            self._process.kill()
        self._process.join()
        self._process.close()
        self._connection = self._process = None
