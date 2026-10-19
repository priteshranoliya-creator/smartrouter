"""SQLite connection setup and forward-only migrations."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

_MIGRATION_FILE = re.compile(r"^(\d+)_(.+)\.sql$")


def connect(path: Path) -> sqlite3.Connection:
    """Open a WAL-mode connection with foreign keys on. Callers own transactions (autocommit mode)."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply unapplied migrations in version order, one transaction each. Returns versions applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
    )
    done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    pending: list[tuple[int, str, Path]] = []
    for file in migrations_dir.glob("*.sql"):
        match = _MIGRATION_FILE.match(file.name)
        if match is None:
            raise ValueError(f"migration file name must look like 001_name.sql: {file.name}")
        version = int(match.group(1))
        if version not in done:
            pending.append((version, match.group(2), file))
    pending.sort()

    applied: list[int] = []
    for version, name, file in pending:
        try:
            conn.executescript("BEGIN;\n" + file.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, datetime.now(UTC).isoformat()),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied
