import asyncio
from collections import deque

import crawler


def test_is_ignored_path_returns_false_when_no_patterns_configured(monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "IGNORE_PATHS", [])
    assert not crawler.is_ignored_path("https://example.com/archives")


def test_is_ignored_path_bare_entry_excludes_subtree_but_not_sibling(monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "IGNORE_PATHS", ["/archives"])
    assert crawler.is_ignored_path("https://example.com/archives")
    assert crawler.is_ignored_path("https://example.com/archives/notice")
    assert not crawler.is_ignored_path("https://example.com/archives1")


def test_is_ignored_path_wildcard_matches_raw_prefix(monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "IGNORE_PATHS", ["/archives*"])
    assert crawler.is_ignored_path("https://example.com/archives1")
    assert not crawler.is_ignored_path("https://example.com/other")


def test_safe_enqueue_skips_ignored_path_and_never_fetches_it(monkeypatch):
    # This is the real fetch-avoidance gate: it must actually stop the
    # crawl from visiting the path, not just hide it from a report later.
    monkeypatch.setitem(crawler.CONFIG, "IGNORE_PATHS", ["/archives"])
    queue = deque()
    seen = set()
    added = crawler.safe_enqueue(queue, seen, "https://example.com/archives/old", 1)
    assert added is False
    assert len(queue) == 0


def test_safe_enqueue_allows_sibling_path(monkeypatch):
    monkeypatch.setitem(crawler.CONFIG, "IGNORE_PATHS", ["/archives"])
    queue = deque()
    seen = set()
    added = crawler.safe_enqueue(queue, seen, "https://example.com/archives1", 1)
    assert added is True
    assert len(queue) == 1


class _FakeResponse:
    def __init__(self, status: int, text: str, headers: dict | None = None):
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def text(self, errors: str = "ignore") -> str:
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Minimal stand-in for aiohttp.ClientSession.get() as used by
    _seed_from_sitemap — keyed by exact URL, 404 for anything unmapped."""

    def __init__(self, responses: dict[str, _FakeResponse]):
        self._responses = responses

    def get(self, url, **kwargs):
        return self._responses.get(url, _FakeResponse(404, ""))


def test_seed_from_sitemap_skips_ignored_paths(monkeypatch):
    # Root cause of the reported bug: sitemap seeding writes straight to
    # seen/queue, bypassing safe_enqueue() entirely — so before this fix,
    # an ignored path listed in sitemap.xml was fetched anyway even
    # though the exact same path found via normal link discovery was
    # correctly skipped.
    monkeypatch.setitem(crawler.CONFIG, "IGNORE_PATHS", ["/archives"])
    # normalize() canonicalizes www vs non-www against CONFIG["BASE_URL"];
    # pin it to this test's own domain so URLs aren't silently rewritten
    # onto whatever BASE_URL happens to be configured elsewhere.
    monkeypatch.setitem(crawler.CONFIG, "BASE_URL", "https://example.com")
    base = "https://example.com"
    sitemap_xml = (
        "<urlset>"
        "<url><loc>https://example.com/keep</loc></url>"
        "<url><loc>https://example.com/archives/old-notice</loc></url>"
        "</urlset>"
    )
    session = _FakeSession({
        f"{base}/sitemap.xml": _FakeResponse(
            200, sitemap_xml, {"Content-Type": "application/xml"}
        ),
    })
    queue: deque = deque()
    seen: set = set()

    added = asyncio.run(crawler._seed_from_sitemap(base, queue, seen, session))

    urls = [u for u, _ in queue]
    assert added == 1
    assert "https://example.com/keep" in urls
    assert not any("archives" in u for u in urls)
