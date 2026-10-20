import pytest

import crawler


def test_no_results_exits_non_zero_so_ecs_marks_the_task_failed(monkeypatch, tmp_path):
    async def empty_crawl():
        return [], [], {}

    monkeypatch.setitem(crawler.CONFIG, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setitem(crawler.CONFIG, "TARGET_PAGES", [])
    monkeypatch.setattr(crawler, "crawl", empty_crawl)

    import asyncio
    with pytest.raises(SystemExit) as exc:
        asyncio.run(crawler.main())
    assert exc.value.code not in (0, None)


def _page(status):
    return {"link_type": "Page", "status": status}


def test_unreachable_start_page_counts_as_a_failed_crawl():
    assert crawler.crawl_failed([_page("Connection Error")]) is True
    assert crawler.crawl_failed([_page("404")]) is True


def test_a_normal_run_with_broken_links_is_not_a_failure():
    rows = [_page("200"), {"link_type": "Internal", "status": "404"}]
    assert crawler.crawl_failed(rows) is False
