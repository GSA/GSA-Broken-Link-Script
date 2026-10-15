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
  -e BASE_URL="https://bravent.us/" -e MAX_PAGES=30 -e MAX_DEPTH=2 \
  -e SCAN_LABEL="bravent" \
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
  `OUTPUT_DIR` per site (e.g. `/output/bravent`).
- **Logs:** stdout/stderr, unbuffered, so they stream to CloudWatch Logs as-is.
- **Exit code:** the crawler exits 0 even when the site is unreachable (it still
  writes a report), so ECS shows that task as successful. Don't rely on the exit
  code alone to detect a failed run.
- **Upload:** with `REPORT_BUCKET` set, every file in `OUTPUT_DIR` is uploaded to
  `s3://$REPORT_BUCKET/<SCAN_LABEL>/<timestamp>/` after the reports are written.
  Unset for local runs. A failed upload fails the task.
- **Google Sheet:** with `GOOGLE_DRIVE_CREDENTIALS` (service-account key JSON,
  injected from Secrets Manager) and `GOOGLE_DRIVE_FOLDER_ID` set, the `.xlsx` is
  also uploaded to that Drive folder and converted to a native Google Sheet, so
  cells can be commented on and people @-tagged. It runs after the S3 upload, so
  the report is stored even if Drive fails; a Drive failure then fails the task.
  An empty or `{}` credential (the placeholder) skips it. Use a Shared Drive or
  a folder shared with the service account's email. Google Sheets caps a file at
  10 million cells, so a very large crawl may not convert.
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
start a crawl straight away. The workflow needs `AWS_ACCESS_KEY_ID_DEV` /
`AWS_SECRET_ACCESS_KEY_DEV` repository secrets.

## Status

Being moved to a monthly ECS Fargate run in dev, with reports in S3. Still to
do: Google Drive upload, a separate tab for bot-blocked links (401/403/429),
and failure alerting.
