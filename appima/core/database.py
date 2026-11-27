"""SQLite persistence layer for managed applications."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from appima.core.models import AppRecord

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS apps (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    format TEXT NOT NULL,
    provider TEXT NOT NULL,
    source_url TEXT NOT NULL,
    asset_pattern TEXT NOT NULL DEFAULT '',
    installed_version TEXT NOT NULL,
    available_version TEXT NOT NULL DEFAULT '',
    available_download_url TEXT NOT NULL DEFAULT '',
    available_checksum TEXT NOT NULL DEFAULT '',
    available_checksum_algo TEXT NOT NULL DEFAULT '',
    install_path TEXT NOT NULL,
    backup_path TEXT NOT NULL DEFAULT '',
    backup_version TEXT NOT NULL DEFAULT '',
    desktop_file TEXT NOT NULL DEFAULT '',
    icon_path TEXT NOT NULL DEFAULT '',
    checksum_algo TEXT NOT NULL DEFAULT '',
    checksum_value TEXT NOT NULL DEFAULT '',
    checksum_verified INTEGER NOT NULL DEFAULT 0,
    etag TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    launch_args TEXT NOT NULL DEFAULT '',
    env_vars TEXT NOT NULL DEFAULT '',
    release_url TEXT NOT NULL DEFAULT '',
    release_notes TEXT NOT NULL DEFAULT '',
    installed_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    last_checked_at TEXT NOT NULL DEFAULT ''
);
"""

_COLUMNS = (
    "id",
    "name",
    "format",
    "provider",
    "source_url",
    "asset_pattern",
    "installed_version",
    "available_version",
    "available_download_url",
    "available_checksum",
    "available_checksum_algo",
    "install_path",
    "backup_path",
    "backup_version",
    "desktop_file",
    "icon_path",
    "checksum_algo",
    "checksum_value",
    "checksum_verified",
    "etag",
    "last_modified",
    "launch_args",
    "env_vars",
    "release_url",
    "release_notes",
    "installed_at",
    "updated_at",
    "last_checked_at",
)


class Database:
    """Thin, thread-safe SQLite wrapper storing AppRecord rows."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AppRecord:
        data = dict(row)
        data["checksum_verified"] = bool(data["checksum_verified"])
        return AppRecord(**data)

    def list_apps(self) -> list[AppRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM apps ORDER BY LOWER(name) ASC"
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_app(self, app_id: str) -> AppRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM apps WHERE id = ?", (app_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def find_by_install_path(self, install_path: str | Path) -> AppRecord | None:
        resolved = str(Path(install_path).expanduser().resolve())
        for app in self.list_apps():
            try:
                if str(Path(app.install_path).expanduser().resolve()) == resolved:
                    return app
            except OSError:
                if app.install_path == str(install_path):
                    return app
        return None

    def upsert_app(self, record: AppRecord) -> None:
        placeholders = ", ".join("?" for _ in _COLUMNS)
        updates = ", ".join(f"{col} = excluded.{col}" for col in _COLUMNS if col != "id")
        values = [
            int(getattr(record, col)) if col == "checksum_verified" else getattr(record, col)
            for col in _COLUMNS
        ]
        sql = (
            f"INSERT INTO apps ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        with self._connect() as conn:
            conn.execute(sql, values)

    def delete_app(self, app_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))
            return cur.rowcount > 0
