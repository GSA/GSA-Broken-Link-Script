import openpyxl
import pytest

import crawler


def _row(link_type, status, url="http://example.com/x"):
    return {
        "page_url": "http://example.com/", "link_url": url, "link_text": "x",
        "link_type": link_type, "status": status, "final_url": url,
        "load_ms": 10, "depth": 0, "effort": "None", "category": "",
        "page_ip": "1.2.3.4", "link_ip": "1.2.3.4",
        "timestamp": "2026-10-01 06:00:00", "resource_type": "Link",
    }


def _sheet_urls(path, title):
    wb = openpyxl.load_workbook(path)
    return [r[1].value for r in wb[title].iter_rows(min_row=2)]


def test_external_bot_blocks_get_their_own_tab_and_leave_broken_links(tmp_path):
    path = str(tmp_path / "r.xlsx")
    crawler.write_excel([
        _row("External", "403", "http://other.com/blocked"),
        _row("External", "429", "http://other.com/limited"),
        _row("External", "404", "http://other.com/gone"),
        _row("Internal", "403", "http://example.com/forbidden"),
        _row("Internal", "404", "http://example.com/missing"),
    ], path)

    assert sorted(_sheet_urls(path, "Blocked by bot protection")) == [
        "http://other.com/blocked", "http://other.com/limited"]
    assert sorted(_sheet_urls(path, "Broken links")) == [
        "http://example.com/forbidden", "http://example.com/missing",
        "http://other.com/gone"]
    # the full picture is unchanged: everything non-200 stays in this tab
    assert len(_sheet_urls(path, "All broken links")) == 5


def test_blocked_count_is_in_the_summary(tmp_path):
    path = str(tmp_path / "r.xlsx")
    crawler.write_excel([_row("External", "403")], path)
    ws = openpyxl.load_workbook(path)["Summary"]
    vals = {r[0].value: r[1].value for r in ws.iter_rows() if r[0].value}
    key = next(k for k in vals if str(k).startswith("External blocked"))
    assert vals[key] == 1
