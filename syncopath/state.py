"""SQLite state database for tracking file sync state."""
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import STATE_DIR

log = logging.getLogger(__name__)


@dataclass
class FileState:
    path: str  # relative path from sync root
    file_id: str  # Google Drive file ID
    remote_md5: Optional[str]  # md5 from Drive (None for Google Docs)
    local_md5: Optional[str]  # md5 of local file at last sync
    remote_mtime: str  # ISO timestamp from Drive
    local_mtime: float  # local file mtime at last sync
    mime_type: str
    is_folder: bool = False
    sync_status: str = "unknown"  # synced, syncing, pending, error, unknown


class StateDB:
    """SQLite-backed sync state tracker."""

    def __init__(self, account_name: str):
        self.db_path = STATE_DIR / f"{account_name}.db"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent reads while worker thread writes
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                file_id TEXT UNIQUE,
                remote_md5 TEXT,
                local_md5 TEXT,
                remote_mtime TEXT,
                local_mtime REAL,
                mime_type TEXT,
                is_folder INTEGER DEFAULT 0,
                sync_status TEXT DEFAULT 'unknown'
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_file_id ON files(file_id);
        """)
        # Migration: add sync_status column if missing (existing DBs)
        try:
            self._conn.execute("SELECT sync_status FROM files LIMIT 1")
        except sqlite3.OperationalError:
            self._conn.execute("ALTER TABLE files ADD COLUMN sync_status TEXT DEFAULT 'unknown'")
        self._conn.commit()

    def get_page_token(self) -> Optional[str]:
        """Get stored Changes API page token."""
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = 'page_token'"
        ).fetchone()
        return row["value"] if row else None

    def set_page_token(self, token: str):
        """Store Changes API page token."""
        self._conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('page_token', ?)",
            (token,),
        )
        self._conn.commit()

    def get_by_path(self, path: str) -> Optional[FileState]:
        """Look up file state by relative path."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE path = ?", (path,)
        ).fetchone()
        return self._row_to_state(row) if row else None

    def get_by_file_id(self, file_id: str) -> Optional[FileState]:
        """Look up file state by Drive file ID."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (file_id,)
        ).fetchone()
        return self._row_to_state(row) if row else None

    def get_all(self) -> list[FileState]:
        """Get all tracked files."""
        rows = self._conn.execute("SELECT * FROM files").fetchall()
        return [self._row_to_state(r) for r in rows]

    def upsert(self, state: FileState):
        """Insert or update file state."""
        self._conn.execute(
            """INSERT OR REPLACE INTO files
               (path, file_id, remote_md5, local_md5, remote_mtime, local_mtime, mime_type, is_folder, sync_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                state.path,
                state.file_id,
                state.remote_md5,
                state.local_md5,
                state.remote_mtime,
                state.local_mtime,
                state.mime_type,
                int(state.is_folder),
                state.sync_status,
            ),
        )
        self._conn.commit()

    def delete_by_path(self, path: str):
        """Remove a file from state tracking."""
        self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self._conn.commit()

    def delete_by_file_id(self, file_id: str):
        """Remove a file from state tracking by its Drive ID."""
        self._conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
        self._conn.commit()

    def rename(self, old_path: str, new_path: str):
        """Update path after a rename/move."""
        self._conn.execute(
            "UPDATE files SET path = ? WHERE path = ?", (new_path, old_path)
        )
        self._conn.commit()

    def rename_prefix(self, old_prefix: str, new_prefix: str):
        """Update all paths that start with old_prefix to use new_prefix.

        Used when a folder is renamed/moved — all children need their paths updated.
        """
        old_prefix_slash = old_prefix + "/"
        rows = self._conn.execute(
            "SELECT path FROM files WHERE path LIKE ?", (old_prefix_slash + "%",)
        ).fetchall()
        for row in rows:
            old_path = row["path"]
            updated_path = new_prefix + "/" + old_path[len(old_prefix_slash):]
            self._conn.execute(
                "UPDATE files SET path = ? WHERE path = ?", (updated_path, old_path)
            )
        self._conn.commit()
        if rows:
            log.debug("Renamed %d children from '%s/' to '%s/'",
                      len(rows), old_prefix, new_prefix)

    def set_sync_status(self, path: str, status: str):
        """Update sync_status for a file."""
        self._conn.execute(
            "UPDATE files SET sync_status = ? WHERE path = ?", (status, path)
        )
        self._conn.commit()

    def get_all_paths_with_status(self) -> dict[str, str]:
        """Get a dict of {path: sync_status} for all tracked files."""
        rows = self._conn.execute(
            "SELECT path, sync_status FROM files WHERE is_folder = 0"
        ).fetchall()
        return {row["path"]: row["sync_status"] for row in rows}

    def clear(self):
        """Clear all state (for resync)."""
        self._conn.execute("DELETE FROM files")
        self._conn.execute("DELETE FROM metadata")
        self._conn.commit()

    def close(self):
        self._conn.close()

    def _row_to_state(self, row: sqlite3.Row) -> FileState:
        return FileState(
            path=row["path"],
            file_id=row["file_id"],
            remote_md5=row["remote_md5"],
            local_md5=row["local_md5"],
            remote_mtime=row["remote_mtime"],
            local_mtime=row["local_mtime"],
            mime_type=row["mime_type"],
            is_folder=bool(row["is_folder"]),
            sync_status=row["sync_status"] if "sync_status" in row.keys() else "unknown",
        )
