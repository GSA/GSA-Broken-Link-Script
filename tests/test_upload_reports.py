from pathlib import Path

import crawler


class _FakeS3:
    def __init__(self):
        self.uploads = []

    def upload_file(self, filename, bucket, key):
        self.uploads.append((Path(filename).name, bucket, key))


def test_uploads_every_output_file_under_the_prefix(tmp_path):
    (tmp_path / "broken_links_x.xlsx").write_text("x")
    (tmp_path / "index.html").write_text("x")
    s3 = _FakeS3()

    crawler.upload_reports(tmp_path, "reports-bucket", "site/20261001_060000", s3=s3)

    assert s3.uploads == [
        ("broken_links_x.xlsx", "reports-bucket", "site/20261001_060000/broken_links_x.xlsx"),
        ("index.html", "reports-bucket", "site/20261001_060000/index.html"),
    ]
