import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from smartrouter.app import DASHBOARD_DIR, create_app
from tests.integration.test_proxy import settings_with_db

PANELS = [
    "panel-feed",
    "panel-spend",
    "panel-ledger",
    "panel-cache",
    "panel-mix",
    "panel-quality",
    "panel-eval",
]
ASSET = re.compile(r'(?:src|href)="(/ui/assets/[^"]+)"')


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app: FastAPI = create_app(settings_with_db(tmp_path / "router.db"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://smartrouter.test") as c,
    ):
        yield c


async def served_assets(client: httpx.AsyncClient) -> dict[str, httpx.Response]:
    page = await client.get("/")
    return {path: await client.get(path) for path in ASSET.findall(page.text)}


async def test_root_serves_the_built_index(client: httpx.AsyncClient) -> None:
    res = await client.get("/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert res.text == (DASHBOARD_DIR / "index.html").read_text()
    assert 'id="app"' in res.text
    assert "cdn.jsdelivr.net" not in res.text
    assert len(ASSET.findall(res.text)) == 2


async def test_hashed_assets_are_served_not_proxied(client: httpx.AsyncClient) -> None:
    assets = await served_assets(client)
    by_type = {path.rsplit(".", 1)[1]: res for path, res in assets.items()}
    assert set(by_type) == {"js", "css"}
    for path, res in assets.items():
        assert res.status_code == 200, path
        assert res.content == (DASHBOARD_DIR / path.removeprefix("/ui/")).read_bytes()
    assert "javascript" in by_type["js"].headers["content-type"]
    assert "text/css" in by_type["css"].headers["content-type"]


async def test_the_bundle_renders_every_panel(client: httpx.AsyncClient) -> None:
    # The page is client-rendered, so the panel ids live in the bundle, not in the index.
    (script,) = [r for p, r in (await served_assets(client)).items() if p.endswith(".js")]
    for panel in PANELS:
        assert f'"{panel}"' in script.text, panel


async def test_unknown_asset_under_the_base_path_is_404_not_proxied(client: httpx.AsyncClient) -> None:
    res = await client.get("/ui/assets/missing.js")
    assert res.status_code == 404


def test_the_old_static_files_are_gone() -> None:
    assert not (DASHBOARD_DIR.parent / "app.js").exists()
    assert not (DASHBOARD_DIR.parent / "styles.css").exists()


def test_sources_never_inject_html_from_data() -> None:
    sources = [p for p in (DASHBOARD_DIR.parent / "src").rglob("*") if p.suffix in {".ts", ".tsx"}]
    assert sources
    for path in sources:
        text = path.read_text()
        for banned in ("dangerouslySetInnerHTML", "innerHTML", "insertAdjacentHTML"):
            assert banned not in text, f"{banned} in {path}"
