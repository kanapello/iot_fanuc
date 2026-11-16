"""SQLite store-and-forward buffer.

Every message is written here before it is published and removed only after
the broker acknowledged it (PUBACK). The whole envelope is stored, so a
resend carries the same ``msg_id`` and ``ts``. The per-machine ``seq``
counters live in the same database and survive restarts.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    payload BLOB NOT NULL,
    retain INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seq (
    key TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class BufferedMessage:
    id: int
    topic: str
    payload: bytes
    retain: bool


class Buffer:
    def __init__(self, path: str, max_age_days: float = 7) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._max_age_s = max_age_days * 86400
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)

    def next_seq(self, key: str) -> int:
        with self._lock:
            row = self._db.execute(
                "INSERT INTO seq (key, next_seq) VALUES (?, 2) "
                "ON CONFLICT (key) DO UPDATE SET next_seq = next_seq + 1 RETURNING next_seq - 1",
                (key,),
            ).fetchone()
        return int(row[0])

    def add(self, topic: str, payload: bytes, retain: bool = False) -> int:
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO messages (topic, payload, retain, created_at) VALUES (?, ?, ?, ?)",
                (topic, payload, int(retain), time.time()),
            )
        return int(cursor.lastrowid or 0)

    def pending(self, after_id: int = 0, limit: int = 200) -> list[BufferedMessage]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, topic, payload, retain FROM messages WHERE id > ? ORDER BY id LIMIT ?",
                (after_id, limit),
            ).fetchall()
        return [BufferedMessage(int(r[0]), str(r[1]), bytes(r[2]), bool(r[3])) for r in rows]

    def delete(self, message_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM messages WHERE id = ?", (message_id,))

    def size(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT count(*) FROM messages").fetchone()[0])

    def evict_expired(self) -> int:
        """Drop messages older than ``max_age_days`` (oldest data is sacrificed first)."""
        with self._lock:
            cursor = self._db.execute("DELETE FROM messages WHERE created_at < ?", (time.time() - self._max_age_s,))
        return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()
