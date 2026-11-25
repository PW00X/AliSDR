"""Where decoded messages go: a ring in memory for the UI and one JSON-lines file per day on disk."""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from ..config import atomic_write_text


class EventHub:
    def __init__(self, directory: Path | None, keep_days: int = 7, ring: int = 3000, stats=None):
        self._dir = directory
        self.stats = stats          # StatsStore: every event is counted there, link frames too
        self.on_event = None        # called with every message worth reading (not link frames): the Notifier
        self._keep_days = keep_days
        self._lock = threading.Lock()
        self._ring: deque[dict] = deque(maxlen=ring)
        self._link_ring: deque[dict] = deque(maxlen=600)    # link-layer frames flood the ring: they get a small one of their own
        self._next_id = int(time.time() * 1000)      # a page open across a restart must not wait for the new numbers to pass its old ones
        self._pruned_on: date | None = None
        self._generation = uuid4().hex
        self._load_today()

    def _load_today(self) -> None:
        if self._dir is None:
            return
        path = self._dir / f"events-{date.today().isoformat()}.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        start = max(0, len(lines) - self._ring.maxlen)
        day = path.stem[7:]
        for index, line in enumerate(lines[start:], start):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            event.setdefault("message_key", f"{day}/old-{index}")
            event["id"] = self._next_id
            self._next_id += 1
            self._ring.append(event)

    def publish(self, addon: str, event: dict, summary: str) -> dict:
        event = {**event, "addon": addon, "ts": round(time.time(), 3), "summary": summary}
        if self.stats is not None:
            self.stats.record(event)
        # Link-layer frames (VDL2 acknowledgements and polls) are most of the traffic and nobody reads them: they are
        # counted in the statistics but neither kept on disk nor shown beyond the live ring.
        persist = not (event.get("kind") == "vdl2" and event.get("layer") == "link")
        with self._lock:
            event["message_key"] = f"{date.today().isoformat()}/{uuid4().hex}"
            event["id"] = self._next_id
            self._next_id += 1
            (self._ring if persist else self._link_ring).append(event)
            if persist:
                self._append(event)
        if persist:
            if self.on_event is not None:
                try:
                    self.on_event(event)
                except Exception:  # noqa: BLE001 - a listener must never stop a decoder
                    pass
        return event

    def since(self, after_id: int = 0, addon: str = "", limit: int = 500) -> list[dict]:
        return self.snapshot(after_id, addon, limit)["events"]

    def clear(self) -> str:
        """Clear all retained event logs and both live buffers as one operation with respect to publishers."""
        with self._lock:
            if self._dir is not None:
                for path in self._dir.glob("events-????-??-??.jsonl"):
                    path.unlink(missing_ok=True)
            self._ring.clear()
            self._link_ring.clear()
            self._generation = uuid4().hex
            return self._generation

    def delete_page(self, keys: list) -> str:
        if not isinstance(keys, list) or not 1 <= len(keys) <= 3600:
            raise ValueError("Invalid message selection")
        by_day = {}
        for key in keys:
            if not isinstance(key, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}/(?:[0-9a-f]{32}|old-[0-9]{1,12})", key):
                raise ValueError("Invalid message key")
            by_day.setdefault(key.split("/", 1)[0], set()).add(key)
        with self._lock:
            for day, selected in by_day.items():
                if self._dir is not None:
                    self._delete_from_log(self._dir / f"events-{day}.jsonl", day, selected)
                for ring in (self._ring, self._link_ring):
                    remaining = [event for event in ring if event["message_key"] not in selected]
                    ring.clear()
                    ring.extend(remaining)
                self._generation = uuid4().hex
            return self._generation

    @staticmethod
    def _delete_from_log(path: Path, day: str, selected: set) -> None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except FileNotFoundError:
            return
        kept = []
        removed = False
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
            except ValueError:
                kept.append(line)
                continue
            if isinstance(event, dict):
                # Older logs have no key. Preserve the original line identity when compacting.
                key = event.get("message_key", f"{day}/old-{index}")
                if key in selected:
                    removed = True
                    continue
                if "message_key" not in event:
                    event["message_key"] = key
                    line = json.dumps(event, ensure_ascii=False) + "\n"
            kept.append(line)
        if removed:
            atomic_write_text(path, "".join(kept))

    def snapshot(self, after_id: int, addon: str, limit: int, generation: str = "") -> dict:
        with self._lock:
            if generation and generation != self._generation:
                after_id = 0
            current = self._generation
            rows = [e for e in (*self._ring, *self._link_ring) if e["id"] > after_id and (not addon or e["addon"] == addon)]
        rows.sort(key=lambda e: e["id"])
        return {"events": rows[-limit:], "generation": current}

    def _append(self, event: dict) -> None:
        if self._dir is None:
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            day = event["message_key"].split("/", 1)[0]
            with open(self._dir / f"events-{day}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({k: v for k, v in event.items() if k != "id"}, ensure_ascii=False) + "\n")
            self._prune()
        except OSError:
            pass     # a full disk must not stop the decoder

    def _prune(self) -> None:
        today = date.today()
        if self._pruned_on == today or self._dir is None:
            return
        self._pruned_on = today
        oldest = today - timedelta(days=self._keep_days)
        for path in self._dir.glob("events-*.jsonl"):
            try:
                if date.fromisoformat(path.stem[7:]) < oldest:
                    path.unlink()
            except (ValueError, OSError):
                pass
