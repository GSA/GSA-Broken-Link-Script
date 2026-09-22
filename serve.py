"""
serve.py — Report browser for crawler.py output.

Serves the entire reports folder over HTTP and shows a navigation
page listing every scan report, JSON data file, CSV, and Excel file.

Usage
-----
  python serve.py                       # ./reports on port 8088
  python serve.py 9000                  # custom port
  python serve.py 9000 ./my-reports     # custom port + custom folder

Mac / Linux / Windows — no extra dependencies needed.
"""

import http.server
import socketserver
import webbrowser
import os
import sys
from pathlib import Path
from datetime import datetime
from urllib.parse import unquote

# ─────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────
PORT        = int(sys.argv[1])   if len(sys.argv) > 1 else 8088
REPORTS_DIR = Path(sys.argv[2])  if len(sys.argv) > 2 else Path("./reports")

if not REPORTS_DIR.exists():
    print(f"ERROR: Directory not found: {REPORTS_DIR.resolve()}")
    sys.exit(1)

os.chdir(REPORTS_DIR)


# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────

def _fmt_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.0f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} GB"


def _fmt_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def _scan_reports(base: Path) -> dict:
    """Walk the reports directory and group files into categories."""
    groups: dict = {
        "reports": [],
        "manual":  [],
        "skipped": [],
        "excel":   [],
        "csv":     [],
        "data":    [],
        "other":   [],
    }

    try:
        items = sorted(base.iterdir(), key=lambda x: -x.stat().st_mtime)
    except Exception:
        return groups

    for p in items:
        if not p.is_file():
            continue
        name  = p.name
        lower = name.lower()

        try:
            sz = p.stat().st_size
        except Exception:
            sz = 0

        info = {
            "name":  name,
            "path":  name,
            "size":  _fmt_size(sz),
            "mtime": _fmt_mtime(p),
        }

        if lower == "index.html":
            # Always first: link opens it, but also show size/date
            info["label"] = "Latest report (index.html) — always the most recent scan"
            groups["reports"].insert(0, info)

        elif lower.startswith("report_") and lower.endswith(".html"):
            # Timestamped copies: report_YYYYMMDD_HHMMSS[_label].html
            ts_part = name[len("report_"):-len(".html")]
            # Try to parse timestamp for a nice label
            parts = ts_part.split("_")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                date_str = parts[0]      # YYYYMMDD
                time_str = parts[1]      # HHMMSS
                label_part = "_".join(parts[2:]) if len(parts) > 2 else ""
                try:
                    dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
                    nice = dt.strftime("%Y-%m-%d %H:%M:%S")
                    info["label"] = (nice + "  " + label_part).strip()
                except Exception:
                    info["label"] = ts_part
            else:
                info["label"] = ts_part
            groups["reports"].append(info)

        elif lower == "skipped_urls.html":
            groups["skipped"].append(info)

        elif lower.endswith(".html"):
            info["label"] = name
            groups["reports"].append(info)

        elif lower.startswith("manual_url_scan") and lower.endswith(".xlsx"):
            groups["manual"].append(info)

        elif lower.startswith("manual_url_scan") and lower.endswith(".html"):
            info["label"] = name
            groups["manual"].append(info)

        elif lower.startswith("report_data_") and lower.endswith(".json"):
            groups["data"].append(info)

        elif lower.startswith("broken_links_") and lower.endswith(".csv"):
            groups["csv"].append(info)

        elif lower.startswith("broken_links_") and lower.endswith(".xlsx"):
            groups["excel"].append(info)

        elif lower == "dropped_pages.txt":
            groups["other"].append(info)

        elif not lower.endswith((".json",)):  # skip data JSON from other list
            groups["other"].append(info)

    return groups


# ─────────────────────────────────────────────────────────────
#  Navigation page HTML
# ─────────────────────────────────────────────────────────────

def _file_rows(items: list, is_html: bool = False) -> str:
    if not items:
        return (
            '<tr><td colspan="3" style="color:#4b5563;font-style:italic;'
            'padding:10px 12px">No files found</td></tr>'
        )
    out = ""
    for it in items:
        label = it.get("label", it["name"])
        href  = "/" + it["path"]
        if is_html:
            link = (
                f'<a href="{href}" target="_blank" '
                f'style="color:#60a5fa;text-decoration:none;font-weight:600">'
                f'{label}</a>'
            )
        else:
            link = (
                f'<a href="{href}" download '
                f'style="color:#93c5fd;text-decoration:none">'
                f'&#8659; {it["name"]}</a>'
            )
        out += (
            f'<tr>'
            f'<td style="padding:8px 14px">{link}</td>'
            f'<td style="padding:8px 14px;color:#6b7280;font-size:12px;'
            f'white-space:nowrap">{it["mtime"]}</td>'
            f'<td style="padding:8px 14px;color:#6b7280;font-size:12px;'
            f'text-align:right;white-space:nowrap">{it["size"]}</td>'
            f'</tr>'
        )
    return out


def _section(title: str, rows_html: str, count: int) -> str:
    return f"""
<div style="margin-bottom:28px">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
    <h2 style="font-size:14px;font-weight:700;color:#e2e8f0;margin:0">{title}</h2>
    <span style="background:#334155;color:#94a3b8;padding:1px 8px;
                 border-radius:10px;font-size:11px">{count}</span>
  </div>
  <div style="background:#1e293b;border:1px solid #334155;border-radius:8px;overflow:hidden">
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="border-bottom:1px solid #334155">
          <th style="padding:8px 14px;text-align:left;color:#64748b;
                     font-size:11px;text-transform:uppercase;letter-spacing:.05em">File</th>
          <th style="padding:8px 14px;text-align:left;color:#64748b;
                     font-size:11px;text-transform:uppercase;letter-spacing:.05em">Modified</th>
          <th style="padding:8px 14px;text-align:right;color:#64748b;
                     font-size:11px;text-transform:uppercase;letter-spacing:.05em">Size</th>
        </tr>
      </thead>
      <tbody style="font-size:13px">{rows_html}</tbody>
    </table>
  </div>
</div>"""


def _nav_page(groups: dict) -> bytes:
    now_str       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_reports = len(groups["reports"])

    sections = ""

    if groups["reports"]:
        sections += _section(
            "Scan Reports",
            _file_rows(groups["reports"], is_html=True),
            len(groups["reports"]),
        )
    if groups["manual"]:
        sections += _section(
            "Manual URL Scan Results",
            _file_rows(groups["manual"]),
            len(groups["manual"]),
        )
    if groups["skipped"]:
        sections += _section(
            "Skipped URLs Reports",
            _file_rows(groups["skipped"], is_html=True),
            len(groups["skipped"]),
        )
    if groups["excel"]:
        sections += _section(
            "Excel Reports",
            _file_rows(groups["excel"]),
            len(groups["excel"]),
        )
    if groups["csv"]:
        sections += _section(
            "CSV Reports",
            _file_rows(groups["csv"]),
            len(groups["csv"]),
        )
    if groups["data"]:
        sections += _section(
            "Data Files (JSON)",
            _file_rows(groups["data"]),
            len(groups["data"]),
        )
    if groups["other"]:
        sections += _section(
            "Other Files",
            _file_rows(groups["other"]),
            len(groups["other"]),
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crawler Report Browser</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0f172a;color:#e2e8f0;
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       font-size:14px;min-height:100vh}}
  tbody tr:hover{{background:#263348}}
  tbody tr{{border-bottom:1px solid #1e293b}}
  a:hover{{text-decoration:underline!important}}
</style>
</head>
<body>
<div style="background:#1e293b;border-bottom:1px solid #334155;
            padding:16px 32px;display:flex;align-items:center;
            justify-content:space-between;flex-wrap:wrap;gap:10px">
  <div>
    <h1 style="font-size:1.2rem;font-weight:700">Crawler Report Browser</h1>
    <div style="color:#64748b;font-size:12px;margin-top:3px">
      Folder: <code style="color:#93c5fd">{REPORTS_DIR.resolve()}</code>
      &nbsp;|&nbsp; {now_str}
    </div>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <span style="background:#0369a1;color:#fff;padding:2px 10px;
                 border-radius:12px;font-size:12px;font-weight:700">
      {total_reports} scan report{"" if total_reports == 1 else "s"} found
    </span>
    <a href="/"
       style="background:#334155;color:#93c5fd;padding:5px 14px;
              border-radius:6px;font-size:12px;text-decoration:none;font-weight:600">
      Refresh
    </a>
    <a href="/index.html" target="_blank"
       style="background:#1d4ed8;color:#fff;padding:5px 14px;
              border-radius:6px;font-size:12px;text-decoration:none;font-weight:600">
      Open Latest (index.html) &#8599;
    </a>
  </div>
</div>

<div style="max-width:1100px;margin:0 auto;padding:28px 32px">
  <div style="background:#1e293b;border:1px solid #1d4ed8;border-radius:10px;
              padding:12px 16px;margin-bottom:24px;font-size:13px;color:#93c5fd">
    Click any <b>Scan Report</b> row to open it in a new tab.
    Reports load their data from a paired <code>report_data_*.json</code> file —
    they must be opened here (not by double-clicking the .html file directly).
    <br>
    <span style="color:#64748b;font-size:12px">
      Each crawler run saves <code>index.html</code> (always latest) plus a
      <code>report_YYYYMMDD_HHMMSS.html</code> timestamped copy so you can
      compare past scans. Refresh this page after a new scan to see it appear.
    </span>
  </div>
  {sections}
</div>
</body>
</html>"""
    return html.encode("utf-8")


# ─────────────────────────────────────────────────────────────
#  Custom HTTP handler
# ─────────────────────────────────────────────────────────────

class ReportHandler(http.server.SimpleHTTPRequestHandler):

    def do_GET(self):
        path = unquote(self.path.split("?")[0])
        # Always serve the nav page for root
        if path in ("/", ""):
            self._serve_nav()
        else:
            super().do_GET()

    def _serve_nav(self):
        groups  = _scan_reports(REPORTS_DIR)
        content = _nav_page(groups)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt, *args):
        # Suppress noisy asset requests
        path = str(args[0]) if args else ""
        for ext in (".json", ".ico", ".png", ".woff", ".woff2"):
            if path.endswith(ext):
                return
        super().log_message(fmt, *args)


# ─────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print(f"\n  Crawler Report Browser")
    print(f"  {'=' * 44}")
    print(f"  Reports folder : {REPORTS_DIR.resolve()}")
    print(f"  Browser URL    : {url}")
    print(f"  Press Ctrl+C to stop.\n")

    webbrowser.open(url)

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), ReportHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Server stopped.")
