import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from smartrouter.app import DASHBOARD_DIR, create_app
from tests.integration.test_proxy import settings_with_db

CHART_JS = "https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js"
PANELS = ["panel-feed", "panel-spend", "panel-ledger", "panel-cache", "panel-mix", "panel-quality"]


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app: FastAPI = create_app(settings_with_db(tmp_path / "router.db"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://smartrouter.test") as c,
    ):
        yield c


async def test_root_serves_the_dashboard_page(client: httpx.AsyncClient) -> None:
    res = await client.get("/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert CHART_JS in res.text
    for panel in PANELS:
        assert f'id="{panel}"' in res.text


@pytest.mark.parametrize(
    ("path", "content_type"),
    [("/static/app.js", "javascript"), ("/static/styles.css", "text/css")],
)
async def test_static_assets_are_served_not_proxied(
    client: httpx.AsyncClient, path: str, content_type: str
) -> None:
    res = await client.get(path)
    assert res.status_code == 200
    assert content_type in res.headers["content-type"]
    assert res.text == (DASHBOARD_DIR / path.removeprefix("/static/")).read_text()


def test_app_js_writes_prompt_text_with_textcontent() -> None:
    source = (DASHBOARD_DIR / "app.js").read_text()
    assert re.search(r'"prompt-text",\s*text:\s*snippet\(turn\.prompt', source)
    assert "innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert 'key === "text") node.textContent' in source
