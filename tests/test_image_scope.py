import crawler


def test_check_images_config_defaults_to_true():
    assert crawler.CONFIG["CHECK_IMAGES"] is True


def test_fields_list_ends_with_resource_type():
    assert crawler.FIELDS[-1] == "resource_type"


def _reset():
    crawler._reset_structural_cache()


def test_extract_images_zoned_pulls_src_and_normalizes(monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "BASE_URL", "http://example.com")
    _reset()
    html = '<html><body><img src="/logo.png" alt="Logo"></body></html>'
    imgs = crawler.extract_images_zoned(html, "http://example.com/", is_home=True)
    assert imgs == [("http://example.com/logo.png", "Logo")]


def test_extract_images_zoned_pulls_srcset_and_drops_descriptors(monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "BASE_URL", "http://example.com")
    _reset()
    html = (
        '<html><body>'
        '<img src="/hero.jpg" srcset="/hero-480.jpg 480w, /hero-800.jpg 800w" alt="Hero">'
        '</body></html>'
    )
    imgs = crawler.extract_images_zoned(html, "http://example.com/", is_home=True)
    urls = [u for u, _ in imgs]
    assert "http://example.com/hero.jpg" in urls
    assert "http://example.com/hero-480.jpg" in urls
    assert "http://example.com/hero-800.jpg" in urls


def test_extract_images_zoned_drops_data_uri_images(monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "BASE_URL", "http://example.com")
    _reset()
    html = '<html><body><img src="data:image/gif;base64,R0lGOD" alt="placeholder"></body></html>'
    imgs = crawler.extract_images_zoned(html, "http://example.com/", is_home=True)
    assert imgs == []


def test_extract_images_zoned_suppresses_header_image_on_second_page(monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "BASE_URL", "http://example.com")
    _reset()
    home_html = (
        '<html><body><header><img src="/logo.png" alt="Logo"></header>'
        '<main><img src="/hero.jpg" alt="Hero"></main></body></html>'
    )
    page2_html = (
        '<html><body><header><img src="/logo.png" alt="Logo"></header>'
        '<main><img src="/content2.jpg" alt="C2"></main></body></html>'
    )
    home_imgs = crawler.extract_images_zoned(home_html, "http://example.com/", is_home=True)
    page2_imgs = crawler.extract_images_zoned(page2_html, "http://example.com/page2", is_home=False)

    assert ("http://example.com/logo.png", "Logo") in home_imgs
    assert not any(u == "http://example.com/logo.png" for u, _ in page2_imgs)
    assert ("http://example.com/content2.jpg", "C2") in page2_imgs


def test_content_image_is_checked_on_every_page_it_appears_on(monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "BASE_URL", "http://example.com")
    _reset()
    page1_html = '<html><body><main><img src="/product.jpg" alt="Product"></main></body></html>'
    page2_html = '<html><body><main><img src="/product.jpg" alt="Product"></main></body></html>'
    imgs1 = crawler.extract_images_zoned(page1_html, "http://example.com/p1", is_home=False)
    imgs2 = crawler.extract_images_zoned(page2_html, "http://example.com/p2", is_home=False)
    assert imgs1 == [("http://example.com/product.jpg", "Product")]
    assert imgs2 == [("http://example.com/product.jpg", "Product")]


def test_structural_link_suppresses_the_same_url_as_a_structural_image(monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "BASE_URL", "http://example.com")
    _reset()
    home_html = (
        '<html><body>'
        '<header><a href="/about">About</a></header>'
        '<main><img src="/hero.jpg" alt="Hero"></main>'
        '</body></html>'
    )
    page2_html = '<html><body><header><img src="/about" alt="About banner"></header></body></html>'

    crawler.extract_links_zoned(home_html, "http://example.com/", is_home=True)
    page2_imgs = crawler.extract_images_zoned(page2_html, "http://example.com/page2", is_home=False)
    assert not any(u == "http://example.com/about" for u, _ in page2_imgs)


def test_extract_images_plain_parser_extracts_src_and_drops_data_uri(monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "BASE_URL", "http://example.com")
    html = '<img src="/logo.png" alt="Logo"><img src="data:image/gif;base64,xx" alt="ph">'
    imgs = crawler.extract_images(html, "http://example.com/")
    assert imgs == [("http://example.com/logo.png", "Logo")]


import asyncio
from urllib.robotparser import RobotFileParser

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

IMG_PAGES = {
    "/": (
        '<header><img src="/logo.png" alt="Logo"></header>'
        '<main><img src="/hero.jpg" alt="Hero"><a href="/page2">two</a></main>'
    ),
    "/page2": (
        '<header><img src="/logo.png" alt="Logo"></header>'
        '<main><img src="/broken.jpg" alt="Broken"></main>'
    ),
}


def _make_image_app() -> web.Application:
    app = web.Application()

    async def page_handler(request):
        body = IMG_PAGES.get(request.path)
        if body is None:
            return web.Response(status=404, text="not found")
        return web.Response(text=f"<html><body>{body}</body></html>", content_type="text/html")

    async def image_handler(request):
        return web.Response(body=b"\x89PNG", content_type="image/png")

    async def missing_handler(request):
        return web.Response(status=404, text="not found")

    for path in list(IMG_PAGES) + ["/robots.txt", "/sitemap.xml"]:
        app.router.add_get(path, page_handler)
    app.router.add_get("/logo.png", image_handler)
    app.router.add_get("/hero.jpg", image_handler)
    app.router.add_get("/broken.jpg", missing_handler)
    return app


def _allow_all_robots(base_url: str) -> RobotFileParser:
    rp = RobotFileParser()
    rp.allow_all = True
    return rp


@pytest.fixture
def image_crawl_config(monkeypatch, tmp_path):
    monkeypatch.setitem(crawler.CONFIG, "MAX_DEPTH", 10)
    monkeypatch.setitem(crawler.CONFIG, "MAX_PAGES", 0)
    monkeypatch.setitem(crawler.CONFIG, "CONCURRENCY", 5)
    monkeypatch.setitem(crawler.CONFIG, "TIMEOUT", 5)
    monkeypatch.setitem(crawler.CONFIG, "SEED_SITEMAP", False)
    monkeypatch.setitem(crawler.CONFIG, "RESPECT_ROBOTS", False)
    monkeypatch.setitem(crawler.CONFIG, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setitem(crawler.CONFIG, "IGNORE_PATHS", [])
    crawler._reset_structural_cache()
    return tmp_path


async def _run_image_crawl(monkeypatch):
    monkeypatch.setattr(crawler, "load_robots", _allow_all_robots)
    server = TestServer(_make_image_app())
    await server.start_server()
    try:
        base_url = str(server.make_url("/")).rstrip("/")
        monkeypatch.setitem(crawler.CONFIG, "BASE_URL", base_url)
        results, _, _ = await crawler.crawl()
    finally:
        await server.close()
    return results


def test_check_images_true_produces_image_rows_including_a_broken_one(image_crawl_config, monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "CHECK_IMAGES", True)
    results = asyncio.run(_run_image_crawl(monkeypatch))
    image_rows = [r for r in results if r.get("resource_type") == "Image"]
    assert image_rows, "no image rows were produced"
    broken = [r for r in image_rows if r["link_url"].endswith("/broken.jpg")]
    assert broken and str(broken[0]["status"]) == "404"


def test_check_images_false_produces_zero_image_rows(image_crawl_config, monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "CHECK_IMAGES", False)
    results = asyncio.run(_run_image_crawl(monkeypatch))
    assert not any(r.get("resource_type") == "Image" for r in results)


def test_header_image_checked_once_not_once_per_page(image_crawl_config, monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "CHECK_IMAGES", True)
    results = asyncio.run(_run_image_crawl(monkeypatch))
    logo_rows = [r for r in results if r["link_url"].endswith("/logo.png")]
    assert len(logo_rows) == 1


def test_broken_image_never_enqueued_as_a_page_to_crawl(image_crawl_config, monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "CHECK_IMAGES", True)
    results = asyncio.run(_run_image_crawl(monkeypatch))
    page_self_urls = {r["page_url"] for r in results if r.get("link_text") == "(page itself)"}
    assert not any(u.endswith((".jpg", ".png")) for u in page_self_urls)


def test_targeted_crawl_also_checks_images_when_enabled(image_crawl_config, monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "CHECK_IMAGES", True)

    async def _run():
        monkeypatch.setattr(crawler, "load_robots", _allow_all_robots)
        server = TestServer(_make_image_app())
        await server.start_server()
        try:
            base_url = str(server.make_url("/")).rstrip("/")
            monkeypatch.setitem(crawler.CONFIG, "BASE_URL", base_url)
            return await crawler.targeted_crawl([base_url])
        finally:
            await server.close()

    results = asyncio.run(_run())
    assert any(r.get("resource_type") == "Image" for r in results)


def test_html_report_table_has_resource_column():
    html = crawler.build_html_report([], "x.csv", 1.0, inline_data=[])
    assert '<th onclick="sortBy(12)" title="Link or Image">Resource</th>' in html


def test_html_report_sort_keys_include_resource_type():
    html = crawler.build_html_report([], "x.csv", 1.0, inline_data=[])
    assert (
        "const SORT_KEYS = ['pu','lu','lt','tp','st','ca','dp','ef','lm','pi','li','ts','rt'];"
        in html
    )


def test_html_report_row_template_renders_resource_cell():
    html = crawler.build_html_report([], "x.csv", 1.0, inline_data=[])
    assert "esc(r.rt||'Link')" in html
