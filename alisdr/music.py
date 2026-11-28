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
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS music (
                station_id TEXT, track_key TEXT, station TEXT, track TEXT,
                plays INTEGER, last_heard REAL, last_seen REAL, PRIMARY KEY (station_id, track_key))""")
            if "last_seen" not in {col[1] for col in db.execute("PRAGMA table_info(music)")}:
                db.execute("ALTER TABLE music ADD COLUMN last_seen REAL")
            if not self._has_history(db):
                db.execute("""CREATE TABLE music_history (
                    id INTEGER PRIMARY KEY, track_key TEXT NOT NULL, station TEXT NOT NULL, heard REAL NOT NULL)""")
                db.execute("CREATE INDEX music_history_track ON music_history(track_key, heard DESC, id DESC)")
                # Recover the last known play per station from older installations.
                db.execute("""INSERT INTO music_history(track_key, station, heard)
                    SELECT track_key, station, last_heard FROM (
                        SELECT *, row_number() OVER (PARTITION BY track_key ORDER BY last_heard DESC, station_id) AS n
                        FROM music) WHERE n <= 50""")
            key = track.casefold()
            # A repeat extends the quiet period without refreshing the displayed play.
            repeat = db.execute("""UPDATE music SET last_seen=MAX(COALESCE(last_seen, last_heard), ?)
                WHERE station_id=? AND track_key=? AND COALESCE(last_seen, last_heard)>?""",
                (heard, station_id, key, heard - 30 * 60))
            if repeat.rowcount:
                return
            db.execute("INSERT INTO music_history(track_key, station, heard) VALUES (?, ?, ?)", (key, station, heard))
            db.execute("""DELETE FROM music_history WHERE id IN
                (SELECT id FROM music_history WHERE track_key=? ORDER BY heard DESC, id DESC LIMIT -1 OFFSET 50)""", (key,))
            db.execute("""INSERT INTO music (station_id, track_key, station, track, plays, last_heard, last_seen)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(station_id, track_key) DO UPDATE SET
                station=excluded.station, plays=plays+1, last_heard=excluded.last_heard, last_seen=excluded.last_seen""",
                (station_id, key, station, track, heard, heard))

    @staticmethod
    def _has_history(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE name='music_history'").fetchone()

    def latest_tracks(self, stations):
        if not stations or not self.path.exists():
            return {}
        with closing(sqlite3.connect(self.path)) as db:
            # One indexed station lookup per favourite; names can change or coincide.
            result = {}
            for station in set(stations):
                row = db.execute("""SELECT track, last_heard FROM music WHERE station_id=?
                    ORDER BY last_heard DESC, rowid DESC LIMIT 1""", (station,)).fetchone()
                if row:
                    result[station] = {"track": row[0], "heard": row[1]}
            return result

    def history(self, track):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            if self._has_history(db):
                query = "SELECT station, heard FROM music_history WHERE track_key=? ORDER BY heard DESC, id DESC LIMIT 50"
            else:
                query = "SELECT station, last_heard AS heard FROM music WHERE track_key=? ORDER BY last_heard DESC, station_id LIMIT 50"
            return [dict(row) for row in db.execute(query, (track.casefold(),))]

    def clear(self):
        if self.path.exists():
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("DELETE FROM music")
                if self._has_history(db):
                    db.execute("DELETE FROM music_history")

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
