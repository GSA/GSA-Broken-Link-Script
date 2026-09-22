import asyncio
from urllib.robotparser import RobotFileParser

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import crawler


def test_check_links_config_defaults_to_true():
    assert crawler.CONFIG["CHECK_LINKS"] is True


LINK_PAGES = {
    "/": '<main><a href="/page2">Page 2</a><a href="/broken">Broken</a></main>',
    "/page2": '<main><a href="/page3">Page 3</a></main>',
    "/page3": '<main>No more links</main>',
}


def _make_link_app() -> web.Application:
    app = web.Application()

    async def page_handler(request):
        body = LINK_PAGES.get(request.path)
        if body is None:
            return web.Response(status=404, text="not found")
        return web.Response(text=f"<html><body>{body}</body></html>", content_type="text/html")

    for path in list(LINK_PAGES) + ["/robots.txt", "/sitemap.xml"]:
        app.router.add_get(path, page_handler)
    return app


def _allow_all_robots(base_url: str) -> RobotFileParser:
    rp = RobotFileParser()
    rp.allow_all = True
    return rp


@pytest.fixture
def link_crawl_config(monkeypatch, tmp_path):
    monkeypatch.setitem(crawler.CONFIG, "MAX_DEPTH", 10)
    monkeypatch.setitem(crawler.CONFIG, "MAX_PAGES", 0)
    monkeypatch.setitem(crawler.CONFIG, "CONCURRENCY", 5)
    monkeypatch.setitem(crawler.CONFIG, "TIMEOUT", 5)
    monkeypatch.setitem(crawler.CONFIG, "SEED_SITEMAP", False)
    monkeypatch.setitem(crawler.CONFIG, "RESPECT_ROBOTS", False)
    monkeypatch.setitem(crawler.CONFIG, "CHECK_IMAGES", False)
    monkeypatch.setitem(crawler.CONFIG, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setitem(crawler.CONFIG, "IGNORE_PATHS", [])
    crawler._reset_structural_cache()
    return tmp_path


async def _run_link_crawl(monkeypatch):
    monkeypatch.setattr(crawler, "load_robots", _allow_all_robots)
    server = TestServer(_make_link_app())
    await server.start_server()
    try:
        base_url = str(server.make_url("/")).rstrip("/")
        monkeypatch.setitem(crawler.CONFIG, "BASE_URL", base_url)
        results, _, _ = await crawler.crawl()
    finally:
        await server.close()
    return results


def test_check_links_true_produces_link_rows_including_a_broken_one(link_crawl_config, monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "CHECK_LINKS", True)
    results = asyncio.run(_run_link_crawl(monkeypatch))
    link_rows = [r for r in results if r.get("resource_type") == "Link"]
    assert link_rows, "no link rows were produced"
    broken = [r for r in link_rows if r["link_url"].endswith("/broken")]
    assert broken and str(broken[0]["status"]) == "404"


def test_check_links_false_produces_zero_link_rows_but_still_traverses_the_site(link_crawl_config, monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "CHECK_LINKS", False)
    results = asyncio.run(_run_link_crawl(monkeypatch))

    assert not any(r.get("resource_type") == "Link" for r in results)

    page_itself_urls = {r["link_url"] for r in results if r["link_text"] == "(page itself)"}
    assert any(u.endswith("/page2") for u in page_itself_urls)
    assert any(u.endswith("/page3") for u in page_itself_urls)
    broken_page = [r for r in results if r["link_text"] == "(page itself)" and r["link_url"].endswith("/broken")]
    assert broken_page and str(broken_page[0]["status"]) == "404"


def test_check_links_false_with_check_images_true_still_checks_images(link_crawl_config, monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "CHECK_LINKS", False)
    monkeypatch.setitem(crawler.CONFIG, "CHECK_IMAGES", True)

    async def _run():
        monkeypatch.setattr(crawler, "load_robots", _allow_all_robots)
        app = web.Application()

        pages = {
            "/": '<main><img src="/logo.png" alt="Logo"><a href="/page2">Page 2</a></main>',
            "/page2": '<main>No more links</main>',
        }

        async def page_handler(request):
            body = pages.get(request.path)
            if body is None:
                return web.Response(status=404, text="not found")
            return web.Response(text=f"<html><body>{body}</body></html>", content_type="text/html")

        async def image_handler(request):
            return web.Response(body=b"\x89PNG", content_type="image/png")

        for path in list(pages) + ["/robots.txt", "/sitemap.xml"]:
            app.router.add_get(path, page_handler)
        app.router.add_get("/logo.png", image_handler)

        server = TestServer(app)
        await server.start_server()
        try:
            base_url = str(server.make_url("/")).rstrip("/")
            monkeypatch.setitem(crawler.CONFIG, "BASE_URL", base_url)
            return await crawler.crawl()
        finally:
            await server.close()

    results, _, _ = asyncio.run(_run())
    assert any(r.get("resource_type") == "Image" for r in results)
    assert not any(r.get("resource_type") == "Link" for r in results)


def test_targeted_crawl_check_links_false_skips_link_rows(link_crawl_config, monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "CHECK_LINKS", False)

    async def _run():
        monkeypatch.setattr(crawler, "load_robots", _allow_all_robots)
        server = TestServer(_make_link_app())
        await server.start_server()
        try:
            base_url = str(server.make_url("/")).rstrip("/")
            monkeypatch.setitem(crawler.CONFIG, "BASE_URL", base_url)
            return await crawler.targeted_crawl([base_url])
        finally:
            await server.close()

    results = asyncio.run(_run())
    assert not any(r.get("resource_type") == "Link" for r in results)
