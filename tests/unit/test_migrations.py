import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from smartrouter.telemetry.db import connect, migrate
from smartrouter.telemetry.models import Decision, Request, Session, Turn
from smartrouter.telemetry.repo import Repo

TABLES = {
    "sessions",
    "session_models",
    "turns",
    "decisions",
    "requests",
    "compactions",
    "eval_runs",
    "eval_results",
    "schema_migrations",
}
INDEXES = {
    "idx_requests_session_created",
    "idx_requests_turn",
    "idx_turns_session_seq",
    "idx_eval_results_run_arm",
}


def _names(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,)).fetchall()
    return {r[0] for r in rows}


def test_fresh_db_has_every_table_and_index(tmp_path: Path) -> None:
    conn = connect(tmp_path / "r.db")
    migrate(conn)
    assert _names(conn, "table") >= TABLES
    assert _names(conn, "index") >= INDEXES


def test_open_creates_missing_parent_dir_and_schema(tmp_path: Path) -> None:
    with Repo.open(tmp_path / "nested" / "dir" / "r.db") as repo:
        assert repo.get_session("nope") is None


def test_migrate_twice_applies_nothing_new(tmp_path: Path) -> None:
    conn = connect(tmp_path / "r.db")
    assert migrate(conn) == [1]
    assert migrate(conn) == []
    rows = conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, "init")]


def test_journal_mode_is_wal(tmp_path: Path) -> None:
    conn = connect(tmp_path / "r.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrations_apply_in_version_order_and_roll_back_on_failure(tmp_path: Path) -> None:
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "002_second.sql").write_text("CREATE TABLE b (x INTEGER);")
    (mdir / "001_first.sql").write_text("CREATE TABLE a (x INTEGER);")
    (mdir / "003_bad.sql").write_text("CREATE TABLE c (x INTEGER); THIS IS NOT SQL;")
    conn = connect(tmp_path / "r.db")
    with pytest.raises(sqlite3.Error):
        migrate(conn, mdir)
    assert {"a", "b"} <= _names(conn, "table")
    assert "c" not in _names(conn, "table")
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == [1, 2]


def _session() -> Session:
    return Session(id="s1", project_path="/p", started_at="2026-10-19T00:00:00+00:00")


def test_bad_rule_is_rejected(tmp_path: Path) -> None:
    with Repo.open(tmp_path / "r.db") as repo:
        repo.upsert_session(_session())
        turn_id = repo.insert_turn(
            Turn(session_id="s1", seq=1, prompt="hi", created_at="2026-10-19T00:00:01+00:00")
        )
        decision = Decision(
            turn_id=turn_id,
            engine="scaledown",
            answers={},
            chosen_model="sonnet",
            rule="payback",
            mode="shadow",
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert_decision(replace(decision, rule=cast(Any, "bogus")))


def test_bad_request_kind_is_rejected(tmp_path: Path) -> None:
    with Repo.open(tmp_path / "r.db") as repo:
        request = Request(
            kind="main",
            requested_model="opus",
            sent_model="opus",
            provider="anthropic",
            cost_usd=0.0,
            created_at="2026-10-19T00:00:00+00:00",
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert_request(replace(request, kind=cast(Any, "bogus")))


def test_turn_for_unknown_session_is_rejected(tmp_path: Path) -> None:
    with Repo.open(tmp_path / "r.db") as repo:
        turn = Turn(session_id="ghost", seq=1, prompt="hi", created_at="2026-10-19T00:00:01+00:00")
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert_turn(turn)


def test_duplicate_turn_seq_in_a_session_is_rejected(tmp_path: Path) -> None:
    with Repo.open(tmp_path / "r.db") as repo:
        repo.upsert_session(_session())
        turn = Turn(session_id="s1", seq=1, prompt="hi", created_at="2026-10-19T00:00:01+00:00")
        repo.insert_turn(turn)
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert_turn(turn)
