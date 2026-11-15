"""Local SQLite state that is not part of the library folders themselves.

Shoot manifests on the library drive are the source of truth for photos and
versions. This database only holds what can be rebuilt or re-checked: cloud
replica status, import receipts, the iPad outbox, review items and caches.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts (
    source_key TEXT PRIMARY KEY, sha256 TEXT NOT NULL, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signatures (sha256 TEXT PRIMARY KEY, frequency TEXT NOT NULL, difference TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY, shoot_id TEXT NOT NULL, photo_id TEXT NOT NULL, file_name TEXT NOT NULL,
    state TEXT NOT NULL, queued_at TEXT NOT NULL, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS trash (
    id TEXT PRIMARY KEY, shoot_id TEXT NOT NULL, relative_path TEXT NOT NULL, trash_path TEXT NOT NULL,
    sha256 TEXT NOT NULL, deleted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drive_files (
    relative_path TEXT PRIMARY KEY, file_id TEXT NOT NULL, sha256 TEXT NOT NULL, verified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drive_folders (relative_path TEXT PRIMARY KEY, folder_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gphotos (
    sha256 TEXT PRIMARY KEY, state TEXT NOT NULL, media_key TEXT, dedup_key TEXT,
    archived INTEGER NOT NULL DEFAULT 0, via TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gphotos_uploads (
    sha256 TEXT PRIMARY KEY, media_item_id TEXT NOT NULL, album_id TEXT, uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gphotos_albums (shoot_id TEXT PRIMARY KEY, album_id TEXT NOT NULL, title TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ghash (sha256 TEXT PRIMARY KEY, google_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_hashes (
    path TEXT PRIMARY KEY, folder TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL, hashed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trash_reasons (trash_id TEXT PRIMARY KEY, reason TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS drive_files_sha ON drive_files(sha256);
CREATE INDEX IF NOT EXISTS source_hashes_folder ON source_hashes(folder);
"""


class StateDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        with self.lock:
            self.connection.executescript(SCHEMA)
            self.connection.commit()

    def execute(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            rows = self.connection.execute(sql, parameters).fetchall()
            self.connection.commit()
            return rows

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        with self.lock:
            self.connection.executemany(sql, rows)
            self.connection.commit()

    def get(self, key: str, default: str = "") -> str:
        rows = self.execute("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))

    def close(self) -> None:
        with self.lock:
            self.connection.close()
