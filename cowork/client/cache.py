from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class CachedProject:
    project_id: str
    project_name: str
    server_url: str
    member_id: str
    member_token: str
    display_name: str
    last_used_at: float
    last_channel_id: Optional[str]


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id     TEXT PRIMARY KEY,
    project_name   TEXT NOT NULL,
    server_url     TEXT NOT NULL,
    member_id      TEXT NOT NULL,
    member_token   TEXT NOT NULL,
    display_name   TEXT NOT NULL,
    last_used_at   REAL NOT NULL,
    last_channel_id TEXT
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class ClientCache:
    """Synchronous SQLite-backed cache for joined projects + UI state."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def upsert_project(
        self,
        project_id: str,
        project_name: str,
        server_url: str,
        member_id: str,
        member_token: str,
        display_name: str,
        last_channel_id: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO projects (project_id, project_name, server_url, member_id,"
            " member_token, display_name, last_used_at, last_channel_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(project_id) DO UPDATE SET"
            " project_name = excluded.project_name,"
            " server_url = excluded.server_url,"
            " member_id = excluded.member_id,"
            " member_token = excluded.member_token,"
            " display_name = excluded.display_name,"
            " last_used_at = excluded.last_used_at,"
            " last_channel_id = COALESCE(excluded.last_channel_id, projects.last_channel_id)",
            (
                project_id,
                project_name,
                server_url,
                member_id,
                member_token,
                display_name,
                time.time(),
                last_channel_id,
            ),
        )
        self._conn.commit()

    def remove_project(self, project_id: str) -> None:
        self._conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        self._conn.commit()

    def list_projects(self) -> list[CachedProject]:
        cur = self._conn.execute(
            "SELECT project_id, project_name, server_url, member_id, member_token,"
            " display_name, last_used_at, last_channel_id FROM projects"
            " ORDER BY last_used_at DESC"
        )
        return [CachedProject(**dict(row)) for row in cur.fetchall()]

    def touch(self, project_id: str, last_channel_id: Optional[str] = None) -> None:
        if last_channel_id is None:
            self._conn.execute(
                "UPDATE projects SET last_used_at = ? WHERE project_id = ?",
                (time.time(), project_id),
            )
        else:
            self._conn.execute(
                "UPDATE projects SET last_used_at = ?, last_channel_id = ? WHERE project_id = ?",
                (time.time(), last_channel_id, project_id),
            )
        self._conn.commit()

    def get_state(self, key: str) -> Optional[str]:
        cur = self._conn.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()
