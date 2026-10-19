import sqlite3
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from smartrouter.app import create_app
from smartrouter.settings import Mode, Settings, load_settings


def settings_with_db(db: Path) -> Settings:
    base = load_settings(env={})
    paths = base.router.paths.model_copy(update={"db": db})
    return replace(base, router=base.router.model_copy(update={"paths": paths, "mode": Mode.OFF}))


def test_startup_creates_a_migrated_db_and_serves_healthz(tmp_path: Path) -> None:
    db = tmp_path / "data" / "router.db"
    with TestClient(create_app(settings_with_db(db))) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    tables = {r[0] for r in sqlite3.connect(db).execute("select name from sqlite_master where type='table'")}
    expected = {
        "sessions",
        "session_models",
        "turns",
        "decisions",
        "requests",
        "compactions",
        "eval_runs",
        "eval_results",
    }
    assert expected <= tables
