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
