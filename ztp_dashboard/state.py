from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


class ZTPState:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "ztp.db"
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS devices (
                        serial      TEXT PRIMARY KEY,
                        model       TEXT,
                        hostname    TEXT,
                        eos_current TEXT,
                        eos_target  TEXT,
                        status      TEXT NOT NULL DEFAULT 'unknown',
                        message     TEXT,
                        last_seen   TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                """)

    def upsert_device(
        self,
        serial: str,
        status: str,
        model: str | None = None,
        hostname: str | None = None,
        eos_current: str | None = None,
        eos_target: str | None = None,
        message: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO devices (serial, model, hostname, eos_current, eos_target, status, message, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(serial) DO UPDATE SET
                        status      = excluded.status,
                        message     = excluded.message,
                        last_seen   = excluded.last_seen,
                        model       = COALESCE(excluded.model, model),
                        hostname    = COALESCE(excluded.hostname, hostname),
                        eos_current = COALESCE(excluded.eos_current, eos_current),
                        eos_target  = COALESCE(excluded.eos_target, eos_target)
                    """,
                    (serial, model, hostname, eos_current, eos_target, status, message, now),
                )

    def all_devices(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM devices ORDER BY last_seen DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_device(self, serial: str) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM devices WHERE serial = ?", (serial,))

    def clear_all_devices(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM devices")

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default
