import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from smartrouter.telemetry.db import MIGRATIONS_DIR, connect, migrate
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
    assert migrate(conn) == [1, 2, 3, 4]
    assert migrate(conn) == []
    rows = conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        (1, "init"),
        (2, "eval_errors"),
        (3, "request_overhead"),
        (4, "decision_error"),
    ]


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


def test_migration_002_adds_error_to_existing_eval_results(tmp_path: Path) -> None:
    only_001 = tmp_path / "m"
    only_001.mkdir()
    (only_001 / "001_init.sql").write_text((MIGRATIONS_DIR / "001_init.sql").read_text())
    conn = connect(tmp_path / "r.db")
    assert migrate(conn, only_001) == [1]
    conn.execute(
        "INSERT INTO eval_runs (dataset_version, arms, judge_model, started_at) VALUES ('v', '[]', 'j', 't')"
    )
    conn.execute(
        "INSERT INTO eval_results (run_id, item_id, arm, model_used, answer) "
        "VALUES (1, 'g-0001', 'a', 'm', 'x')"
    )
    assert migrate(conn) == [2, 3, 4]
    row = conn.execute("SELECT answer, error FROM eval_results").fetchone()
    assert (row["answer"], row["error"]) == ("x", None)
    conn.execute(
        "INSERT INTO eval_results (run_id, item_id, arm, model_used, answer, error) "
        "VALUES (1, 'g-0002', 'a', 'm', '', 'boom')"
    )


def test_migration_003_adds_overhead_to_existing_requests(tmp_path: Path) -> None:
    upto_002 = tmp_path / "m"
    upto_002.mkdir()
    for name in ("001_init.sql", "002_eval_errors.sql"):
        (upto_002 / name).write_text((MIGRATIONS_DIR / name).read_text())
    conn = connect(tmp_path / "r.db")
    assert migrate(conn, upto_002) == [1, 2]
    conn.execute(
        "INSERT INTO requests (kind, requested_model, sent_model, provider, cost_usd, created_at) "
        "VALUES ('main', 'm', 'm', 'anthropic', 0.0, 't')"
    )
    assert migrate(conn) == [3, 4]
    assert conn.execute("SELECT overhead_ms FROM requests").fetchone()["overhead_ms"] is None


def test_migration_004_keeps_decisions_and_accepts_decision_error(tmp_path: Path) -> None:
    upto_003 = tmp_path / "m"
    upto_003.mkdir()
    for name in ("001_init.sql", "002_eval_errors.sql", "003_request_overhead.sql"):
        (upto_003 / name).write_text((MIGRATIONS_DIR / name).read_text())
    conn = connect(tmp_path / "r.db")
    assert migrate(conn, upto_003) == [1, 2, 3]
    conn.execute("INSERT INTO sessions (id, project_path, started_at) VALUES ('s1', '/p', 't')")
    conn.execute("INSERT INTO turns (session_id, seq, prompt, created_at) VALUES ('s1', 1, 'a', 't')")
    for seq in (2, 3):
        conn.execute(
            f"INSERT INTO turns (session_id, seq, prompt, created_at) VALUES ('s1', {seq}, 'b', 't')"
        )
    conn.execute(
        "INSERT INTO decisions (turn_id, engine, answers, chosen_model, rule, mode, est_saving_usd) "
        "VALUES (1, 'scaledown', '{}', 'm', 'payback', 'shadow', 0.5)"
    )
    assert migrate(conn) == [4]
    row = conn.execute("SELECT engine, rule, est_saving_usd FROM decisions").fetchone()
    assert (row["engine"], row["rule"], row["est_saving_usd"]) == ("scaledown", "payback", 0.5)
    conn.execute(
        "INSERT INTO decisions (turn_id, engine, answers, chosen_model, rule, mode) "
        "VALUES (2, 'scaledown', '{}', 'm', 'decision_error', 'shadow')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decisions (turn_id, engine, answers, chosen_model, rule, mode) "
            "VALUES (3, 'scaledown', '{}', 'm', 'bogus', 'shadow')"
        )
