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

## Docker

Crawler-only image (`python:3.12-slim`, non-root user, ~174 MB). No site is baked in:
the target comes from environment variables, so EventBridge/ECS can run one task per
site by overriding them per target.

```bash
docker build -t broken-link-crawler .
docker run --rm \
  -e BASE_URL="https://www.acquisition.gov/" -e MAX_PAGES=30 -e MAX_DEPTH=2 \
  -e SCAN_LABEL="acquisition-gov" \
  -v "$PWD/out:/output" \
  broken-link-crawler
```

- **Per-site config (set by the ECS task/EventBridge override):** `BASE_URL`,
  `SCAN_LABEL`, plus optional `MAX_PAGES`, `MAX_DEPTH`, `CONCURRENCY`, `TIMEOUT`,
  `POLITE_DELAY`, `RESPECT_ROBOTS`, `IGNORE_PATHS`, `CHECK_IMAGES`, `CHECK_LINKS`.
  Defaults are in the `CONFIG` block of `crawler.py`.
- **Output:** everything is written to `OUTPUT_DIR` (`/output` in the image):
  `broken_links_<ts>_<label>.csv/.xlsx`, `report_<ts>_<label>.html`,
  `report_data_<ts>_<label>.json`, plus `index.html` (always the latest run, fixed
  name), `skipped_urls.html`, and `live_results.jsonl` / `live_status.json`.
  Two sites running at once must not share one `/output` (the fixed `index.html`
  and live files would collide): give each task its own volume/path, or set
  `OUTPUT_DIR` per site (e.g. `/output/acquisition-gov`).
- **Logs:** stdout/stderr, unbuffered, so they stream to CloudWatch Logs as-is.
- **Exit code:** exits 0 after a normal run, including one that finds broken
  links. It exits 1 when no crawled page answers 2xx (site unreachable or
  `BASE_URL` wrong; the report is still written and uploaded first), and when
  the S3 upload fails, so ECS marks those tasks
  failed and a failure alert can key off the non-zero exit code.
- **Upload:** with `REPORT_BUCKET` set, the report files in `OUTPUT_DIR` are
  uploaded to `s3://$REPORT_BUCKET/<SCAN_LABEL>/<timestamp>/` after the reports are
  written. Unset for local runs. A failed upload fails the task. To keep a full
  acquisition.gov run near 25 MB instead of about 1 GB, nothing is dropped except
  copies: the CSV is stored as `.csv.gz`, HTML pages are gzipped (browsers still
  open them directly), and three duplicates stay local only (`live_results.jsonl`,
  `report_data_*.json`, and the timestamped `report_*.html`, which has the same
  content as `index.html`).
- **Google Drive:** not used (reports go to S3 only). The upload code is kept
  commented out in `crawler.py` for possible future use; the working version is
  in git history (commit `c795e57`).
- **Report tabs:** `Broken links` lists 4xx on internal links and on external
  links that really are gone. External 401/403/429 go to `Blocked by bot
  protection` instead: most of those open fine in a browser, the target site
  (Cloudflare, Akamai) is just refusing the crawler, so verify by hand.
  `All broken links` still holds every non-200 row.

## AWS

The ECS cluster, task definition, IAM roles, reports bucket and monthly
EventBridge Scheduler schedules (one per site, 1st of the month) live in
[GSA/fcp-ogp-infra](https://github.com/GSA/fcp-ogp-infra) under
`environments/apps/broken-link-crawler`. Crawl pacing (`TIMEOUT`,
`CONCURRENCY`, `POLITE_DELAY`, …) and the site list are set there too.

**Actions → Build, Push and Run Crawler** runs the tests, then pushes the
image to ECR as `:<tag>` and `:latest`. Scheduled tasks always pull `:latest`,
so the next monthly run picks up the new image. Tick `run_now` with a `base_url` to
start a crawl straight away. For a trial run, the optional `max_pages`,
`max_depth`, `concurrency` and `polite_delay` inputs override the scheduled-run
values for that one run; blank keeps the scheduled value (slow: `concurrency` 1,
`polite_delay` 3; fast: 6 and 0.5). Use a test `scan_label` so trial reports
stay apart from the real ones in S3. The workflow needs `AWS_ACCESS_KEY_ID_DEV` /
`AWS_SECRET_ACCESS_KEY_DEV` repository secrets.

## Status

Monthly ECS Fargate run in dev, with reports in S3. The bot-blocked tab
(401/403/429) is built. Still to do: failure alerting on the AWS side.
