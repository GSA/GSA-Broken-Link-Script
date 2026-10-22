import gzip
from pathlib import Path

import crawler


class _FakeS3:
    def __init__(self):
        self.uploads = []
        self.bodies = {}

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.uploads.append((Path(filename).name, bucket, key, ExtraArgs))
        self.bodies[key] = Path(filename).read_bytes()


def test_uploads_report_files_under_the_prefix(tmp_path):
    (tmp_path / "broken_links_x.xlsx").write_text("x")
    (tmp_path / "index.html").write_text("x")
    s3 = _FakeS3()

    keys = crawler.upload_reports(tmp_path, "reports-bucket", "site/20261001_060000", s3=s3)

    assert keys == ["site/20261001_060000/broken_links_x.xlsx",
                    "site/20261001_060000/index.html"]
    assert [(n, b, k) for n, b, k, _ in s3.uploads] == [
        ("broken_links_x.xlsx", "reports-bucket", "site/20261001_060000/broken_links_x.xlsx"),
        ("index.html", "reports-bucket", "site/20261001_060000/index.html"),
    ]


def test_skips_files_that_only_repeat_other_uploaded_data(tmp_path):
    for name in ("live_results.jsonl", "report_20261001_060000_site.html",
                 "report_data_20261001_060000_site.json", "index.html",
                 "skipped_urls.html", "live_status.json"):
        (tmp_path / name).write_text("x")
    s3 = _FakeS3()

    keys = crawler.upload_reports(tmp_path, "b", "p", s3=s3)

    assert sorted(keys) == ["p/index.html", "p/live_status.json", "p/skipped_urls.html"]


def test_csv_is_stored_gzipped_and_loses_nothing(tmp_path):
    rows = "page_url,link_url\n" + "https://a.gov/x,https://a.gov/y\n" * 1000
    (tmp_path / "broken_links_x.csv").write_text(rows)
    s3 = _FakeS3()

    keys = crawler.upload_reports(tmp_path, "b", "p", s3=s3)

    assert keys == ["p/broken_links_x.csv.gz"]
    assert gzip.decompress(s3.bodies[keys[0]]).decode() == rows
    assert len(s3.bodies[keys[0]]) < len(rows)
    assert s3.uploads[0][3] == {"ContentType": "application/gzip"}


def test_html_is_gzipped_with_content_encoding_so_browsers_open_it(tmp_path):
    html = "<html>" + "<tr><td>row</td></tr>" * 1000 + "</html>"
    (tmp_path / "index.html").write_text(html)
    s3 = _FakeS3()

    keys = crawler.upload_reports(tmp_path, "b", "p", s3=s3)

    assert keys == ["p/index.html"]
    assert gzip.decompress(s3.bodies["p/index.html"]).decode() == html
    assert s3.uploads[0][3] == {"ContentType": "text/html; charset=utf-8",
                                "ContentEncoding": "gzip"}


def test_local_files_are_left_untouched(tmp_path):
    (tmp_path / "broken_links_x.csv").write_text("a,b\n1,2\n")
    (tmp_path / "live_results.jsonl").write_text("{}")

    crawler.upload_reports(tmp_path, "b", "p", s3=_FakeS3())

    assert (tmp_path / "broken_links_x.csv").read_text() == "a,b\n1,2\n"
    assert (tmp_path / "live_results.jsonl").exists()
    assert not list(tmp_path.glob("*.gz"))
