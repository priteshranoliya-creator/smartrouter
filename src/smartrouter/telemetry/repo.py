"""Every SQL statement in the app lives here. JSON columns are encoded and decoded only here."""

from __future__ import annotations

import json
import sqlite3
import statistics
import threading
from collections.abc import Collection
from dataclasses import fields
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, Protocol, Self, TypeVar

from smartrouter.telemetry.db import connect, migrate
from smartrouter.telemetry.models import (
    Compaction,
    Decision,
    DecisionFact,
    EvalResult,
    EvalRun,
    Request,
    RequestRow,
    Session,
    SessionModel,
    SessionState,
    Turn,
)


class _RowModel(Protocol):
    __dataclass_fields__: ClassVar[dict[str, Any]]


_R = TypeVar("_R", bound=_RowModel)

_ANSWERS = frozenset({"answers"})
_ARMS = frozenset({"arms"})
_RUBRIC_HITS = frozenset({"rubric_hits"})


def _values(row: _RowModel, json_cols: Collection[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for f in fields(row):
        value = getattr(row, f.name)
        if f.name == "id" and value is None:
            continue
        if f.name in json_cols and value is not None:
            value = json.dumps(value)
        values[f.name] = value
    return values


def _load(cls: type[_R], row: sqlite3.Row, json_cols: Collection[str] = frozenset()) -> _R:
    names = {f.name for f in fields(cls)}
    data = {k: v for k, v in dict(row).items() if k in names}
    for col in json_cols:
        if data[col] is not None:
            data[col] = json.loads(data[col])
    return cls(**data)


class Repo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: Path) -> Repo:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(path)
        migrate(conn)
        return cls(conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _write(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> int | None:
        with self._lock:
            return self._conn.execute(sql, params).lastrowid

    def _write_count(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def _one(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
        return row

    def _all(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        with self._lock:
            rows: list[sqlite3.Row] = self._conn.execute(sql, params).fetchall()
        return rows

    def _insert(self, table: str, row: _RowModel, json_cols: Collection[str] = frozenset()) -> int:
        values = _values(row, json_cols)
        cols = ", ".join(values)
        marks = ", ".join(f":{c}" for c in values)
        rowid = self._write(f"INSERT INTO {table} ({cols}) VALUES ({marks})", values)
        assert rowid is not None
        return rowid

    def _upsert(self, table: str, row: _RowModel, key_cols: tuple[str, ...]) -> None:
        values = _values(row, ())
        cols = ", ".join(values)
        marks = ", ".join(f":{c}" for c in values)
        updates = ", ".join(f"{c} = excluded.{c}" for c in values if c not in key_cols)
        self._write(
            f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
            f"ON CONFLICT ({', '.join(key_cols)}) DO UPDATE SET {updates}",
            values,
        )

    def upsert_session(self, session: Session) -> None:
        self._upsert("sessions", session, ("id",))

    def ensure_session(self, session: Session) -> None:
        """Create the session if unknown. A known one keeps its row, but an empty project_path is filled."""
        self._write(
            "INSERT INTO sessions (id, project_path, started_at, state) "
            "VALUES (:id, :project_path, :started_at, :state) "
            "ON CONFLICT (id) DO UPDATE SET project_path = CASE WHEN sessions.project_path = '' "
            "THEN excluded.project_path ELSE sessions.project_path END",
            {
                "id": session.id,
                "project_path": session.project_path,
                "started_at": session.started_at,
                "state": session.state,
            },
        )

    def get_session(self, session_id: str) -> Session | None:
        row = self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return None if row is None else _load(Session, row)

    def set_session_state(self, session_id: str, state: SessionState) -> None:
        self._write("UPDATE sessions SET state = ? WHERE id = ?", (state, session_id))

    def set_last_switch_turn(self, session_id: str, seq: int) -> None:
        self._write("UPDATE sessions SET last_switch_turn = ? WHERE id = ?", (seq, session_id))

    def mark_session_request(self, session_id: str, model: str, at: str) -> None:
        """A main request finished: it sets the current model and ends a compaction."""
        self._write(
            "UPDATE sessions SET current_model = ?, last_request_at = ?, "
            "state = CASE WHEN state = 'compacting' THEN 'active' ELSE state END WHERE id = ?",
            (model, at, session_id),
        )

    def touch_session_model(self, session_model: SessionModel) -> None:
        self._upsert("session_models", session_model, ("session_id", "model"))

    def get_session_models(self, session_id: str) -> list[SessionModel]:
        rows = self._all("SELECT * FROM session_models WHERE session_id = ? ORDER BY model", (session_id,))
        return [_load(SessionModel, r) for r in rows]

    def get_session_model(self, session_id: str, model: str) -> SessionModel | None:
        row = self._one(
            "SELECT * FROM session_models WHERE session_id = ? AND model = ?", (session_id, model)
        )
        return None if row is None else _load(SessionModel, row)

    def insert_turn(self, turn: Turn) -> int:
        return self._insert("turns", turn)

    def next_turn_seq(self, session_id: str) -> int:
        row = self._one("SELECT COALESCE(MAX(seq), 0) + 1 FROM turns WHERE session_id = ?", (session_id,))
        assert row is not None
        seq: int = row[0]
        return seq

    def get_turn_by_prompt_id(self, session_id: str, prompt_id: str) -> Turn | None:
        row = self._one(
            "SELECT * FROM turns WHERE session_id = ? AND prompt_id = ? ORDER BY seq DESC LIMIT 1",
            (session_id, prompt_id),
        )
        return None if row is None else _load(Turn, row)

    def latest_turn(self, session_id: str) -> Turn | None:
        row = self._one("SELECT * FROM turns WHERE session_id = ? ORDER BY seq DESC LIMIT 1", (session_id,))
        return None if row is None else _load(Turn, row)

    def previous_turn_prompt(self, session_id: str, seq: int) -> str | None:
        row = self._one(
            "SELECT prompt FROM turns WHERE session_id = ? AND seq < ? ORDER BY seq DESC LIMIT 1",
            (session_id, seq),
        )
        return None if row is None else str(row[0])

    def get_turn(self, turn_id: int) -> Turn | None:
        row = self._one("SELECT * FROM turns WHERE id = ?", (turn_id,))
        return None if row is None else _load(Turn, row)

    def list_turns(self, session_id: str) -> list[Turn]:
        rows = self._all("SELECT * FROM turns WHERE session_id = ? ORDER BY seq", (session_id,))
        return [_load(Turn, r) for r in rows]

    def insert_decision(self, decision: Decision) -> None:
        self._insert("decisions", decision, _ANSWERS)

    def insert_decision_if_absent(self, decision: Decision) -> bool:
        """Insert unless the turn already has a decision. True when this call wrote the row."""
        values = _values(decision, _ANSWERS)
        cols = ", ".join(values)
        marks = ", ".join(f":{c}" for c in values)
        return self._write_count(f"INSERT OR IGNORE INTO decisions ({cols}) VALUES ({marks})", values) == 1

    def get_decision(self, turn_id: int) -> Decision | None:
        row = self._one("SELECT * FROM decisions WHERE turn_id = ?", (turn_id,))
        return None if row is None else _load(Decision, row, _ANSWERS)

    def insert_request(self, request: Request) -> int:
        return self._insert("requests", request)

    def main_request_count(self, turn_id: int) -> int:
        row = self._one("SELECT COUNT(*) FROM requests WHERE turn_id = ? AND kind = 'main'", (turn_id,))
        assert row is not None
        count: int = row[0]
        return count

    def turn_fell_back(self, turn_id: int) -> bool:
        """A request of the turn was refused on its routed model and retried as asked."""
        row = self._one(
            "SELECT 1 FROM requests WHERE turn_id = ? AND error LIKE 'fallback:%' LIMIT 1", (turn_id,)
        )
        return row is not None

    def mean_main_output_tokens(self, session_id: str) -> float | None:
        """Mean output tokens of the session's main requests that reported usage, None when there are none."""
        row = self._one(
            "SELECT AVG(output_tokens) FROM requests "
            "WHERE session_id = ? AND kind = 'main' AND output_tokens IS NOT NULL",
            (session_id,),
        )
        assert row is not None
        mean: float | None = row[0]
        return mean

    def turns_per_session(self) -> tuple[float | None, int]:
        """Median turn count over sessions that have turns, and how many such sessions there are."""
        rows = self._all("SELECT COUNT(*) FROM turns GROUP BY session_id", ())
        counts = [r[0] for r in rows]
        return (statistics.median(counts) if counts else None), len(counts)

    def list_requests(self, session_id: str) -> list[Request]:
        rows = self._all("SELECT * FROM requests WHERE session_id = ? ORDER BY created_at, id", (session_id,))
        return [_load(Request, r) for r in rows]

    def insert_compaction(self, compaction: Compaction) -> int:
        return self._insert("compactions", compaction)

    def list_compactions(self, session_id: str) -> list[Compaction]:
        rows = self._all("SELECT * FROM compactions WHERE session_id = ? ORDER BY id", (session_id,))
        return [_load(Compaction, r) for r in rows]

    def insert_eval_run(self, run: EvalRun) -> int:
        return self._insert("eval_runs", run, _ARMS)

    def finish_eval_run(self, run_id: int, finished_at: str) -> None:
        self._write("UPDATE eval_runs SET finished_at = ? WHERE id = ?", (finished_at, run_id))

    def get_eval_run(self, run_id: int) -> EvalRun | None:
        row = self._one("SELECT * FROM eval_runs WHERE id = ?", (run_id,))
        return None if row is None else _load(EvalRun, row, _ARMS)

    def insert_eval_result(self, result: EvalResult) -> int:
        return self._insert("eval_results", result, _RUBRIC_HITS)

    def list_eval_results(self, run_id: int) -> list[EvalResult]:
        rows = self._all("SELECT * FROM eval_results WHERE run_id = ? ORDER BY id", (run_id,))
        return [_load(EvalResult, r, _RUBRIC_HITS) for r in rows]

    def delete_eval_errors(self, run_id: int) -> int:
        """Drop the run's failed rows so a resume can redo them. Returns how many were removed."""
        return self._write_count("DELETE FROM eval_results WHERE run_id = ? AND error IS NOT NULL", (run_id,))

    def write_probe(self) -> bool:
        """True when the database accepts a write transaction (taken and rolled back at once)."""
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            return False
        return True

    def list_sessions(self) -> list[Session]:
        return [_load(Session, r) for r in self._all("SELECT * FROM sessions ORDER BY id", ())]

    def turn_counts(self) -> dict[str, int]:
        rows = self._all("SELECT session_id, COUNT(*) FROM turns GROUP BY session_id", ())
        return {r[0]: r[1] for r in rows}

    def count_turns(self, since: str | None = None) -> int:
        row = self._one("SELECT COUNT(*) FROM turns WHERE (? IS NULL OR created_at >= ?)", (since, since))
        assert row is not None
        count: int = row[0]
        return count

    def count_sessions(self, since: str | None = None) -> int:
        """Sessions with a turn or a request in the window."""
        row = self._one(
            "SELECT COUNT(*) FROM ("
            "SELECT session_id FROM turns WHERE (? IS NULL OR created_at >= ?) "
            "UNION SELECT session_id FROM requests "
            "WHERE session_id IS NOT NULL AND (? IS NULL OR created_at >= ?))",
            (since, since, since, since),
        )
        assert row is not None
        count: int = row[0]
        return count

    def request_rows(self, since: str | None = None, session_id: str | None = None) -> list[RequestRow]:
        """Requests created at or after `since` (ISO UTC), optionally one session's, oldest first."""
        rows = self._all(
            "SELECT r.*, t.seq AS turn_seq, d.mode AS decision_mode, d.rule AS decision_rule, "
            "d.chosen_model AS decision_chosen_model "
            "FROM requests r LEFT JOIN turns t ON t.id = r.turn_id "
            "LEFT JOIN decisions d ON d.turn_id = r.turn_id "
            "WHERE (? IS NULL OR r.created_at >= ?) AND (? IS NULL OR r.session_id = ?) "
            "ORDER BY r.created_at, r.id",
            (since, since, session_id, session_id),
        )
        return [
            RequestRow(
                request=_load(Request, r),
                turn_seq=r["turn_seq"],
                mode=r["decision_mode"],
                rule=r["decision_rule"],
                chosen_model=r["decision_chosen_model"],
            )
            for r in rows
        ]

    def decision_facts(self, since: str | None = None) -> list[DecisionFact]:
        """Decisions of turns created at or after `since`, oldest turn first."""
        rows = self._all(
            "SELECT d.*, t.id AS t_id, t.session_id AS t_session_id, t.seq AS t_seq, "
            "t.prompt_id AS t_prompt_id, t.prompt AS t_prompt, t.override AS t_override, "
            "t.created_at AS t_created_at, "
            "(SELECT r.requested_model FROM requests r WHERE r.turn_id = t.id AND r.kind = 'main' "
            "ORDER BY r.created_at, r.id LIMIT 1) AS first_main_requested_model "
            "FROM decisions d JOIN turns t ON t.id = d.turn_id "
            "WHERE (? IS NULL OR t.created_at >= ?) ORDER BY t.created_at, t.id",
            (since, since),
        )
        return [
            DecisionFact(
                decision=_load(Decision, r, _ANSWERS),
                turn=Turn(
                    id=r["t_id"],
                    session_id=r["t_session_id"],
                    seq=r["t_seq"],
                    prompt_id=r["t_prompt_id"],
                    prompt=r["t_prompt"],
                    override=r["t_override"],
                    created_at=r["t_created_at"],
                ),
                first_main_requested_model=r["first_main_requested_model"],
            )
            for r in rows
        ]

    def session_decisions(self, session_id: str) -> list[Decision]:
        rows = self._all(
            "SELECT d.* FROM decisions d JOIN turns t ON t.id = d.turn_id WHERE t.session_id = ?",
            (session_id,),
        )
        return [_load(Decision, r, _ANSWERS) for r in rows]

    def list_eval_runs(self) -> list[EvalRun]:
        rows = self._all("SELECT * FROM eval_runs ORDER BY id DESC", ())
        return [_load(EvalRun, r, _ARMS) for r in rows]
