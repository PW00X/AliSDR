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
