import json
import re

import crawler


def _sample_results():
    return [{
        "page_url": "http://example.com/",
        "link_url": "http://example.com/a",
        "link_text": "About",
        "link_type": "Internal",
        "status": "200",
        "final_url": "http://example.com/a",
        "load_ms": 12,
        "depth": 0,
        "effort": "None",
        "category": "OK",
        "page_ip": "1.2.3.4",
        "link_ip": "1.2.3.4",
        "timestamp": "2026-08-11 00:00:00",
    }]


def _inline_rows(results):
    return [{
        "pu": r["page_url"], "lu": r["link_url"], "lt": r["link_text"][:120],
        "tp": r["link_type"], "st": r["status"], "fu": r["final_url"],
        "lm": r["load_ms"], "dp": r["depth"], "ef": r["effort"], "ca": r["category"],
        "pi": r["page_ip"], "li": r["link_ip"], "rc": "ok", "ts": r["timestamp"],
    } for r in results]


def test_inline_data_embeds_a_report_data_json_script_tag():
    results = _sample_results()
    html = crawler.build_html_report(results, "dummy.csv", 1.0, inline_data=_inline_rows(results))
    assert 'id="report-data-json"' in html


def test_inline_data_none_falls_back_to_fetch_data_file():
    # A huge crawl's JSON can run hundreds of MB - too big to usefully
    # embed - so the caller passes inline_data=None and the original
    # fetch(DATA_FILE)-over-HTTP path must still work unchanged.
    results = _sample_results()
    html = crawler.build_html_report(results, "dummy.csv", 1.0, inline_data=None)
    assert 'id="report-data-json"' not in html
    assert "fetch(DATA_FILE)" in html


def test_embedded_payload_round_trips_through_json():
    results = _sample_results()
    rows = _inline_rows(results)
    html = crawler.build_html_report(results, "dummy.csv", 1.0, inline_data=rows)
    m = re.search(r'<script type="application/json" id="report-data-json">(.*?)</script>', html, re.S)
    assert m
    payload = json.loads(m.group(1).replace("<\\/", "</"))
    assert payload == rows


def test_link_text_containing_closing_script_tag_cannot_truncate_the_data_block():
    # A crawled page's anchor text is site-controlled content - if it ever
    # contains the literal string "</script>", the browser's HTML tokenizer
    # would otherwise close the data block early regardless of this being
    # valid JSON, silently truncating/corrupting every report from a site
    # whose content happens to include that substring anywhere.
    results = _sample_results()
    results[0]["link_text"] = "click </script><script>alert(1)</script> here"
    rows = _inline_rows(results)
    html = crawler.build_html_report(results, "dummy.csv", 1.0, inline_data=rows)
    m = re.search(r'<script type="application/json" id="report-data-json">(.*?)</script>', html, re.S)
    assert m
    payload_text = m.group(1)
    assert "</script>" not in payload_text
    restored = json.loads(payload_text.replace("<\\/", "</"))
    assert restored[0]["lt"] == "click </script><script>alert(1)</script> here"


def test_loader_prefers_inline_element_over_fetch():
    results = _sample_results()
    html = crawler.build_html_report(results, "dummy.csv", 1.0, inline_data=_inline_rows(results))
    assert "getElementById('report-data-json')" in html
    # The inline branch must be checked before the fetch fallback runs, so a
    # report with embedded data never issues a network request at all.
    assert html.index("getElementById('report-data-json')") < html.index("fetch(DATA_FILE)")
