# GSA-Broken-Link-Script

Async broken-link crawler that produces HTML, CSV, and Excel reports for a site.
Split out of `GSA-Test-Tools/Crawl_script/crawler.py` to be productionized on its
own: containerized, run monthly on AWS (ECS Fargate + EventBridge), with a
manual on-demand trigger via GitHub Actions.

Python 3.9+. Supports macOS, Linux, Windows locally; the container target is Linux.

## Files

- `crawler.py` — the crawler itself. All configuration is environment-variable
  driven (see the `CONFIG` block near the top of the file): `BASE_URL`,
  `MAX_DEPTH`, `MAX_PAGES`, `CONCURRENCY`, `TIMEOUT`, `POLITE_DELAY`,
  `RESPECT_ROBOTS`, `OUTPUT_DIR`, `USER_AGENT`.
- `serve.py` — local-only convenience viewer for the generated HTML report
  (`python serve.py`, opens `http://localhost:8088`). Not part of the container
  pipeline, dev use only.
- `requirements.txt` — `aiohttp`, `beautifulsoup4`, `lxml`, `openpyxl`.
- `tests/` — pytest suite for `crawler.py` (crawl outcomes, ignore-path rules,
  image scope, link-check toggle, Excel/HTML report generation).

## Quick start

```bash
pip install -r requirements.txt
BASE_URL="https://yoursite.gov" python crawler.py
python serve.py   # optional, to view the report locally
```

## Status

In progress: containerizing for a monthly ECS Fargate run (EventBridge
Scheduler trigger, GitHub Actions for manual runs), plus a Google Drive
upload step for the generated report that does not exist in `crawler.py` yet.
