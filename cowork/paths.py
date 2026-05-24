from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir


def data_dir() -> Path:
    override = os.environ.get("COWORK_HOME")
    base = Path(override) if override else Path(user_data_dir("cowork", appauthor=False))
    base.mkdir(parents=True, exist_ok=True)
    return base


def server_db_path() -> Path:
    return data_dir() / "server.db"


def client_db_path() -> Path:
    return data_dir() / "client.db"
