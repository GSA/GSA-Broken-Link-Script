import openpyxl

import crawler


def _sample_results():
    return [{
        "page_url": "http://example.com/",
        "link_url": "http://example.com/a",
        "link_text": "About",
        "link_type": "Internal",
        "status": "404",
        "final_url": "http://example.com/a",
        "load_ms": 12,
        "depth": 0,
        "effort": "High",
        "category": "Not Found",
        "page_ip": "1.2.3.4",
        "link_ip": "1.2.3.4",
        "timestamp": "2026-08-12 16:32:45",
    }]


def _row(page_url, link_type, status):
    return {
        "page_url": page_url,
        "link_url": page_url + "x",
        "link_text": "x",
        "link_type": link_type,
        "status": status,
        "final_url": page_url + "x",
        "load_ms": 10,
        "depth": 0,
        "effort": "None",
        "category": "",
        "page_ip": "1.2.3.4",
        "link_ip": "1.2.3.4",
        "timestamp": "2026-08-12 16:32:45",
    }


def _summary_rows(ws):
    return {row[0].value: row[1].value for row in ws.iter_rows() if row[0].value}


def test_summary_sheet_shows_start_end_and_duration(tmp_path):
    path = str(tmp_path / "report.xlsx")
    crawler.write_excel(
        _sample_results(), path,
        start_dt="2026-08-12 16:32:45",
        end_dt="2026-08-12 16:41:12",
        duration_str="8m 27s",
    )
    wb = openpyxl.load_workbook(path)
    rows = _summary_rows(wb["Summary"])
    assert rows["Start Date and Time"] == "2026-08-12 16:32:45"
    assert rows["End Date and Time"] == "2026-08-12 16:41:12"
    assert rows["Total Run time"] == "8m 27s"


def test_summary_sheet_falls_back_when_timing_not_passed(tmp_path):
    # write_excel() is called elsewhere/by other tests without timing info -
    # must not crash, and should show something rather than a blank cell.
    path = str(tmp_path / "report.xlsx")
    crawler.write_excel(_sample_results(), path)
    wb = openpyxl.load_workbook(path)
    rows = _summary_rows(wb["Summary"])
    assert rows["Start Date and Time"] == "N/A"
    assert rows["Total Run time"] == "N/A"
    assert rows["End Date and Time"]  # falls back to "now", just must be non-empty


def test_summary_sheet_metrics_reconcile_with_4xx_5xx_timeout_split(tmp_path):
    # Previously "Broken (404/410)" only counted 404/410 - a 403 or 400
    # vanished from every named metric even though it was still inside
    # Total Links, which is exactly the "where did the broken links go"
    # confusion. 4xx Errors must now count every 4xx code.
    path = str(tmp_path / "report.xlsx")
    results = [
        _row("http://example.com/", "Internal", "200"),
        _row("http://example.com/", "Internal", "403"),
        _row("http://example.com/", "Internal", "500"),
        _row("http://example.com/", "Internal", "Timeout"),
    ]
    crawler.write_excel(results, path)
    wb = openpyxl.load_workbook(path)
    rows = _summary_rows(wb["Summary"])
    assert rows["4xx Errors"] == 1
    assert rows["Server Errors (5xx)"] == 1
    assert rows["Timeouts / Conn Errors"] == 1
    assert rows["200 OK"] == 1
    assert rows["Total Links"] == 4


def test_summary_sheet_working_broken_matrix_splits_by_link_type(tmp_path):
    path = str(tmp_path / "report.xlsx")
    results = [
        _row("http://example.com/", "Page", "200"),
        _row("http://example.com/", "Internal", "200"),
        _row("http://example.com/", "Internal", "404"),
        _row("http://example.com/", "External", "500"),
        _row("http://example.com/", "External", "301"),
    ]
    crawler.write_excel(results, path)
    wb = openpyxl.load_workbook(path)
    ws = wb["Summary"]
    matrix = {row[0].value: (row[1].value, row[2].value, row[3].value)
              for row in ws.iter_rows()
              if row[0].value in ("Page (self)", "Internal", "External", "All Links")}
    assert matrix["Page (self)"] == (1, 0, 1)
    assert matrix["Internal"] == (1, 1, 2)
    assert matrix["External"] == (1, 1, 2)  # 301 counts as working, not broken
    assert matrix["All Links"] == (3, 2, 5)


def test_pivot_sheet_splits_4xx_5xx_timeout_into_separate_columns(tmp_path):
    path = str(tmp_path / "report.xlsx")
    results = [
        _row("http://example.com/a", "Internal", "404"),
        _row("http://example.com/a", "Internal", "500"),
        _row("http://example.com/a", "Internal", "Timeout"),
    ]
    crawler.write_excel(results, path)
    wb = openpyxl.load_workbook(path)
    ws = wb["Broken by Page"]
    assert [c.value for c in ws[1]] == [
        "Parent URL", "4xx Errors", "5xx Errors",
        "Timeouts / Conn Errors", "Redirects (3xx)", "200 OK", "Total Links",
    ]
    data_row = [c.value for c in ws[2]]
    assert data_row == ["http://example.com/a", 1, 1, 1, 0, 0, 3]


def test_pivot_sheet_has_blank_separator_between_broken_and_working_blocks(tmp_path):
    path = str(tmp_path / "report.xlsx")
    results = [
        _row("http://example.com/broken", "Internal", "404"),
        _row("http://example.com/clean", "Internal", "200"),
    ]
    crawler.write_excel(results, path)
    wb = openpyxl.load_workbook(path)
    ws = wb["Broken by Page"]

    rows = [[c.value for c in row] for row in ws.iter_rows(min_row=2)]
    labels = [r[0] for r in rows]

    assert labels[0] == "http://example.com/broken"
    total_broken_idx = next(i for i, l in enumerate(labels) if str(l).startswith("Total Broken"))
    assert rows[total_broken_idx][1:] == [1, 0, 0, 0, 0, 1]  # 1 broken link, total 1

    blank_idx = total_broken_idx + 1
    assert all(v is None for v in rows[blank_idx]), "separator row must be fully empty for Ctrl+Down"

    working_idx = blank_idx + 1
    assert labels[working_idx] == "http://example.com/clean"

    total_working_idx = next(i for i, l in enumerate(labels) if str(l).startswith("Total Working"))
    assert rows[total_working_idx][1:] == [0, 0, 0, 0, 1, 1]


def test_sheet_order_is_broken_links_then_all_broken_links(tmp_path):
    path = str(tmp_path / "report.xlsx")
    crawler.write_excel(_sample_results(), path)
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames[0] == "Broken links"
    assert wb.sheetnames[1] == "All broken links"


def test_broken_links_sheet_has_4xx_internal_external_only(tmp_path):
    # Actionable-for-the-content-team list: 4xx on an actual link. Excludes
    # the parent page's own status row (link_type="Page" - not a "link"
    # someone edits), and excludes 5xx/timeouts (infrastructure, not
    # something a content team fixes) and 200/3xx (not broken).
    path = str(tmp_path / "report.xlsx")
    results = [
        _row("http://example.com/", "Internal", "404"),
        _row("http://example.com/", "External", "403"),
        _row("http://example.com/", "Page", "404"),
        _row("http://example.com/", "Internal", "500"),
        _row("http://example.com/", "Internal", "Timeout"),
        _row("http://example.com/", "Internal", "200"),
        _row("http://example.com/", "Internal", "301"),
    ]
    crawler.write_excel(results, path)
    wb = openpyxl.load_workbook(path)
    ws = wb["Broken links"]
    statuses = [ws.cell(row=r, column=5).value for r in range(2, ws.max_row + 1)]
    assert sorted(statuses) == ["403", "404"]


def test_broken_links_sheet_has_autofilter_on_header_row(tmp_path):
    path = str(tmp_path / "report.xlsx")
    results = [
        _row("http://example.com/", "Internal", "404"),
        _row("http://example.com/", "External", "403"),
    ]
    crawler.write_excel(results, path)
    wb = openpyxl.load_workbook(path)
    ws = wb["Broken links"]
    assert ws.auto_filter.ref is not None

    ws2 = wb["All broken links"]
    assert ws2.auto_filter.ref is not None


def test_broken_links_sheet_created_even_when_empty(tmp_path):
    path = str(tmp_path / "report.xlsx")
    results = [_row("http://example.com/", "Internal", "200")]
    crawler.write_excel(results, path)
    wb = openpyxl.load_workbook(path)
    assert "Broken links" in wb.sheetnames
    ws = wb["Broken links"]
    assert [c.value for c in ws[1]][0] == "Parent URL"
    assert ws.max_row == 1


def _image_row(page_url, status, link_type="Internal"):
    return {
        "page_url": page_url,
        "link_url": page_url + "img.png",
        "link_text": "alt text",
        "link_type": link_type,
        "status": status,
        "final_url": page_url + "img.png",
        "load_ms": 10,
        "depth": 0,
        "effort": "None",
        "category": "",
        "page_ip": "1.2.3.4",
        "link_ip": "1.2.3.4",
        "timestamp": "2026-08-17 10:00:00",
        "resource_type": "Image",
    }


def test_broken_images_sheet_contains_only_image_rows_4xx_5xx_timeout(tmp_path):
    path = str(tmp_path / "report.xlsx")
    results = [
        _row("http://example.com/", "Internal", "404"),  # broken link, not image - must not appear
        _image_row("http://example.com/", "404"),
        _image_row("http://example.com/", "500"),
        _image_row("http://example.com/", "Timeout"),
        _image_row("http://example.com/", "200"),
        _image_row("http://example.com/", "301"),
    ]
    crawler.write_excel(results, path)
    wb = openpyxl.load_workbook(path)
    ws = wb["Broken images"]
    statuses = [str(ws.cell(row=r, column=5).value) for r in range(2, ws.max_row + 1)]
    assert sorted(statuses) == sorted(["404", "500", "Timeout"])


def test_broken_images_sheet_has_autofilter(tmp_path):
    path = str(tmp_path / "report.xlsx")
    results = [_image_row("http://example.com/", "404")]
    crawler.write_excel(results, path)
    wb = openpyxl.load_workbook(path)
    assert wb["Broken images"].auto_filter.ref is not None


def test_sheet_order_includes_broken_images_between_all_broken_links_and_broken_by_page(tmp_path):
    path = str(tmp_path / "report.xlsx")
    results = [_row("http://example.com/", "Internal", "404"), _image_row("http://example.com/", "404")]
    crawler.write_excel(results, path)
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == [
        "Broken links", "All broken links", "Broken images", "Broken by Page", "Summary",
    ]


def test_image_rows_never_change_the_four_existing_sheets(tmp_path):
    link_only = [
        _row("http://example.com/", "Internal", "404"),
        _row("http://example.com/", "Internal", "200"),
    ]
    with_images = link_only + [
        _image_row("http://example.com/", "404"),
        _image_row("http://example.com/", "200"),
    ]

    path_a = str(tmp_path / "link_only.xlsx")
    path_b = str(tmp_path / "with_images.xlsx")
    crawler.write_excel(link_only, path_a)
    crawler.write_excel(with_images, path_b)

    wb_a = openpyxl.load_workbook(path_a)
    wb_b = openpyxl.load_workbook(path_b)
    for name in ("Broken links", "All broken links", "Broken by Page", "Summary"):
        rows_a = [[c.value for c in row] for row in wb_a[name].iter_rows()]
        rows_b = [[c.value for c in row] for row in wb_b[name].iter_rows()]
        assert rows_a == rows_b, f"{name} sheet changed when image rows were added"
