from fastapi.testclient import TestClient

from smartrouter.app import create_app


def test_healthz_on_empty_app() -> None:
    with TestClient(create_app()) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
