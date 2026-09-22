import asyncio
import json
from urllib.robotparser import RobotFileParser

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import crawler

PAGES = {
    "/": '<a href="/page2">two</a>',
    "/page2": '<a href="/page3">three</a>',
    "/page3": "no more links here",
}


def _make_app() -> web.Application:
    app = web.Application()

    async def handler(request: web.Request) -> web.Response:
        body = PAGES.get(request.path)
        if body is None:
            return web.Response(status=404, text="not found")
        return web.Response(text=f"<html><body>{body}</body></html>", content_type="text/html")

    for path in list(PAGES) + ["/robots.txt", "/sitemap.xml"]:
        app.router.add_get(path, handler)
    return app


@pytest.fixture
def crawl_config(monkeypatch, tmp_path):
    """Points crawler.CONFIG at a tiny local 3-page site and an isolated
    output dir, and restores the run() event loop cleanly afterward."""
    monkeypatch.setitem(crawler.CONFIG, "MAX_DEPTH", 10)
    monkeypatch.setitem(crawler.CONFIG, "CONCURRENCY", 5)
    monkeypatch.setitem(crawler.CONFIG, "TIMEOUT", 5)
    monkeypatch.setitem(crawler.CONFIG, "SEED_SITEMAP", False)
    monkeypatch.setitem(crawler.CONFIG, "RESPECT_ROBOTS", False)
    monkeypatch.setitem(crawler.CONFIG, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setitem(crawler.CONFIG, "IGNORE_PATHS", [])
    return tmp_path


def _allow_all_robots(base_url: str) -> RobotFileParser:
    # load_robots() normally fetches robots.txt via a *synchronous*,
    # blocking urllib call from inside this async crawl — fine against a
    # real remote server, but here client and server share one event
    # loop (this in-process TestServer), so that blocking call would
    # freeze the very loop the server needs to answer it: a deadlock.
    # Not what this test is about, so skip the real fetch entirely.
    rp = RobotFileParser()
    rp.allow_all = True
    return rp


async def _run_crawl_against_test_server(monkeypatch, out_dir, max_pages: int):
    monkeypatch.setattr(crawler, "load_robots", _allow_all_robots)
    server = TestServer(_make_app())
    await server.start_server()
    try:
        base_url = str(server.make_url("/")).rstrip("/")
        monkeypatch.setitem(crawler.CONFIG, "BASE_URL", base_url)
        monkeypatch.setitem(crawler.CONFIG, "MAX_PAGES", max_pages)
        await crawler.crawl()
    finally:
        await server.close()
    return json.loads((out_dir / "live_status.json").read_text())


def test_full_crawl_reports_completed_outcome(crawl_config, monkeypatch):
    status = asyncio.run(
        _run_crawl_against_test_server(monkeypatch, crawl_config, max_pages=0)
    )
    assert status["phase"] == "done"
    assert status["outcome"] == "completed"
    assert status["visited"] == 3
    assert status["queue"] == 0
    assert "ended_display" in status


def test_max_pages_cap_reports_max_pages_cap_outcome(crawl_config, monkeypatch):
    status = asyncio.run(
        _run_crawl_against_test_server(monkeypatch, crawl_config, max_pages=1)
    )
    assert status["phase"] == "done"
    assert status["outcome"] == "max_pages_cap"
    assert status["visited"] == 1
    assert "ended_display" in status
