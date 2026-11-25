"""Track plays identified from broadcast station text."""
import re
import sqlite3
from contextlib import closing

from .config import CONFIG_DIR


def track_label(text):
    text = " ".join(text.split())
    prefix = re.match(r"^(?:now playing|on air now|nu op de radio|nu speelt|nu op [^:]{1,32})\s*:\s*", text, re.I)
    if prefix:
        return text[prefix.end():].strip()
    return text if re.search(r"\S\s+[-–—]\s+\S", text) else ""


class MusicStats:
    def __init__(self, path=None):
        self.path = path or CONFIG_DIR / "music.sqlite3"

    def record(self, station_id, station, track, heard):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS music (
                station_id TEXT, track_key TEXT, station TEXT, track TEXT,
                plays INTEGER, last_heard REAL, PRIMARY KEY (station_id, track_key))""")
            db.execute("""INSERT INTO music VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(station_id, track_key) DO UPDATE SET
                station=excluded.station, plays=plays+1, last_heard=excluded.last_heard""",
                (station_id, track.casefold(), station, track, heard))

    def clear(self):
        if self.path.exists():
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("DELETE FROM music")

    def snapshot(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='music'").fetchone():
                return []
            tracks = {}
            for row in db.execute("""SELECT track_key, station, track, plays, last_heard
                    FROM music ORDER BY last_heard DESC, station, track"""):
                entry = tracks.setdefault(row["track_key"], {
                    "track": row["track"], "plays": 0, "last_heard": row["last_heard"], "stations": {},
                })
                entry["plays"] += row["plays"]
                entry["stations"].setdefault(row["station"].casefold(), row["station"])
            for entry in tracks.values():
                names = sorted(entry.pop("stations").values(), key=str.casefold)
                entry["station"] = ", ".join(names)
            return list(tracks.values())
