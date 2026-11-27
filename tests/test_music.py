import tempfile
import unittest
from pathlib import Path

from alisdr.manual_tuning import ManualSession, Tuning
from alisdr.music import MusicStats


class MusicHistoryTest(unittest.TestCase):
    def test_repeated_labels_station_selection_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "music.sqlite3"
            stats = MusicStats(path)
            self.assertEqual(stats.snapshot(), [])
            session = ManualSession(Tuning(0, "DAB", 223936000, "12A"), {}, False, None, None)
            session.on_music = stats.record
            session._dab_station(1, b"StuBru", 63)
            session.select_station(1)
            for sid, text in [(0, "Ensemble"), (2, "Other - Service"), (1, "Station slogan"),
                              (1, "Artist - First"), (1, "now playing: Artist - First"),
                              (1, "Artist - Second"), (1, "Artist - First")]:
                session._dab_metadata(sid, text.encode())
            session._music_text("FM:100000000", "FM station", "Artist - First")
            counts = {(r["station"], r["track"]): r["plays"] for r in MusicStats(path).snapshot()}
            self.assertEqual(counts, {("FM station, StuBru", "Artist - First"): 3,
                                      ("StuBru", "Artist - Second"): 1})
            stats.clear()
            self.assertEqual(MusicStats(path).snapshot(), [])
            session._music_text("FM:100000000", "FM station", "Artist - New")
            self.assertEqual(stats.snapshot()[0]["plays"], 1)

    def test_history_retention_and_legacy_rows(self):
        import sqlite3
        from contextlib import closing
        with tempfile.TemporaryDirectory() as tmp:
            stats = MusicStats(Path(tmp) / "music.sqlite3")
            stats.record("one", "One", "Track", 0)
            stats.record("other", "Other", "Another track", 1)
            with closing(sqlite3.connect(stats.path)) as db, db:
                db.execute("DROP TABLE music_history")
            self.assertEqual(stats.history("track"), [{"station": "One", "heard": 0}])
            for i in range(1, 55):
                stats.record(str(i % 2), str(i % 2), "Track", i)
            self.assertEqual(stats.history("TRACK"), [
                {"station": str(i % 2), "heard": i} for i in range(54, 4, -1)])
            self.assertEqual(stats.snapshot()[0]["plays"], 55)
            self.assertEqual(len(stats.history("Another track")), 1)
            with closing(sqlite3.connect(stats.path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM music_history").fetchone()[0], 51)
            stats.clear()
            self.assertEqual(stats.history("track"), [])

    def test_monitor_rotates_without_selecting_foreground_or_emitting_audio(self):
        import threading
        from unittest.mock import patch
        from alisdr import manual_tuning
        from alisdr.sdr_backend import SimulatedBackend

        session = ManualSession(Tuning(0, "DAB", 223936000, "12A"),
                                {"driver": "simulated", "gain_mode": "auto", "gain_db": 20,
                                 "async_read": False}, False, lambda _: None, lambda *_: None)
        session.pending_station = 1
        session.monitor_enabled = lambda: True
        heard = {}
        session.on_music = lambda sid, name, track, ts: heard.setdefault(name, []).append(track)
        selected = []
        closed = []
        class FakeDecoder:
            def __init__(self, library, station, reception, metadata, image=None, *, metadata_only=False):
                self.background = metadata_only
                self.metadata = metadata
                self.sid = 0
                if not metadata_only:
                    for sid in range(1, 4):
                        station(sid, str(sid).encode(), 63)

            def select(self, sid):
                self.sid = sid
                if not self.background:
                    selected.append(sid)

            def monitor(self, sid):
                self.other = sid
                return True

            def push(self, iq):
                if getattr(self, "other", 0):
                    self.metadata(self.other, b"Artist - Song")
                if self.sid:
                    self.metadata(self.sid, b"Artist - Song")
                if len(heard) == 3:
                    session.stop_requested.set()

            def drain_audio(self):
                assert not self.background
                return []

            def close(self):
                closed.append(self.background)

        with patch.object(manual_tuning, "Decoder", FakeDecoder), \
                patch.object(manual_tuning, "open_real_backend", return_value=SimulatedBackend(2048000)), \
                patch.object(manual_tuning, "DAB_MONITOR_DWELL_S", .1):
            timer = threading.Timer(4, session.stop_requested.set)
            timer.start()
            try:
                session._dab()
            finally:
                timer.cancel()
        self.assertEqual(selected, [1])
        self.assertEqual(heard, {str(i): ["Artist - Song"] for i in range(1, 4)})
        self.assertEqual(closed, [False])

