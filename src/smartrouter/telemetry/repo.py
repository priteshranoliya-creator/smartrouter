"""Every SQL statement in the app lives here. JSON columns are encoded and decoded only here."""

from __future__ import annotations

import json
import sqlite3
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
    EvalResult,
    EvalRun,
    Request,
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
    data = dict(row)
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

    def get_session(self, session_id: str) -> Session | None:
        row = self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return None if row is None else _load(Session, row)

    def set_session_state(self, session_id: str, state: SessionState) -> None:
        self._write("UPDATE sessions SET state = ? WHERE id = ?", (state, session_id))

    def touch_session_model(self, session_model: SessionModel) -> None:
        self._upsert("session_models", session_model, ("session_id", "model"))

    def get_session_models(self, session_id: str) -> list[SessionModel]:
        rows = self._all("SELECT * FROM session_models WHERE session_id = ? ORDER BY model", (session_id,))
        return [_load(SessionModel, r) for r in rows]

    def insert_turn(self, turn: Turn) -> int:
        return self._insert("turns", turn)

    def get_turn(self, turn_id: int) -> Turn | None:
        row = self._one("SELECT * FROM turns WHERE id = ?", (turn_id,))
        return None if row is None else _load(Turn, row)

    def list_turns(self, session_id: str) -> list[Turn]:
        rows = self._all("SELECT * FROM turns WHERE session_id = ? ORDER BY seq", (session_id,))
        return [_load(Turn, r) for r in rows]

    def insert_decision(self, decision: Decision) -> None:
        self._insert("decisions", decision, _ANSWERS)

    def get_decision(self, turn_id: int) -> Decision | None:
        row = self._one("SELECT * FROM decisions WHERE turn_id = ?", (turn_id,))
        return None if row is None else _load(Decision, row, _ANSWERS)

    def insert_request(self, request: Request) -> int:
        return self._insert("requests", request)

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
