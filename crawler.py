"""
crawler.py — Async broken-link crawler with HTML/CSV/Excel reports.

Supports: macOS · Linux · Windows
Python  : 3.9+

Quick start
-----------
  Mac / Linux:
    BASE_URL="https://example.com" python crawler.py

  Windows (PowerShell):
    $env:BASE_URL="https://example.com"; python crawler.py

  Windows (CMD):
    set BASE_URL=https://example.com && python crawler.py
"""

import asyncio
import aiohttp
import csv
import gzip
import json
import os
import re
import shutil
import signal
import socket
import sys
import tempfile
import time
import logging
from collections import deque, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, urlencode, parse_qs
from urllib.robotparser import RobotFileParser

# ─────────────────────────────────────────────────────────────
#  TIMEZONE  — ZoneInfo (3.9+) with UTC fallback
# ─────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    TZ_EASTERN = ZoneInfo("America/New_York")

    def now_est() -> datetime:
        return datetime.now(TZ_EASTERN)
except Exception:
    # Windows without tzdata package or Python < 3.9
    _UTC_MINUS_5 = timezone(timedelta(hours=-5))

    def now_est() -> datetime:                          # type: ignore[misc]
        return datetime.now(_UTC_MINUS_5)


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


# ─────────────────────────────────────────────────────────────
#  CONFIG  — edit here or override with environment variables
# ─────────────────────────────────────────────────────────────
CONFIG: dict = {
    # --- Core ---
    "BASE_URL":        os.getenv("BASE_URL",    "https://www.example.com"),
    "MAX_DEPTH":       int(os.getenv("MAX_DEPTH",   "10")),
    "MAX_PAGES":       int(os.getenv("MAX_PAGES",   "5000")),
    "CONCURRENCY":     int(os.getenv("CONCURRENCY", "10")),
    "TIMEOUT":         int(os.getenv("TIMEOUT",     "10")),
    "POLITE_DELAY":  float(os.getenv("POLITE_DELAY","0.3")),
    "RESPECT_ROBOTS":  os.getenv("RESPECT_ROBOTS", "true").lower() == "true",
    "OUTPUT_DIR":      os.getenv("OUTPUT_DIR",  "./reports"),
    "USER_AGENT":      os.getenv("USER_AGENT",  "Mozilla/5.0 (compatible; SiteCrawler/1.0)"),

    # --- URL normalisation ---
    # Query params stripped before de-duplication.
    # Includes table-sort params so ?order=X&sort=desc doesn't create duplicates.
    "STRIP_PARAMS": {
        # Analytics
        "utm_source","utm_medium","utm_campaign","utm_content","utm_term",
        # Sessions
        "sessionid","PHPSESSID","sid","ref",
        # Search / CMS
        "search_api_fulltext","search_api_sort",
        # Table sort / order params — FIX: prevents duplicate rows per sorted URL
        "sort","order","sort_by","sort_order","orderby","order_by",
        "direction","sort_dir","sort_field","field_sort","sort_column",
        "sort_key","sortby","sortorder","sort_type","sorted_by",
    },

    # Extensions skipped for link-parsing (HEAD-checked only)
    "SKIP_PARSE_EXTS": {
        ".pdf",".zip",".docx",".xlsx",".pptx",".exe",".dmg",
        ".mp4",".mp3",".avi",".mov",".jpg",".jpeg",".png",
        ".gif",".svg",".webp",".ico",".woff",".woff2",".ttf",
    },

    # Schemes ignored entirely
    "SKIP_SCHEMES": {"mailto","tel","javascript","data","ftp","sms","callto"},

    # Document extensions: HEAD-check only, never crawl for links
    "DOC_EXTS": {
        ".pdf",".doc",".docx",".xls",".xlsx",".ppt",".pptx",
        ".odt",".ods",".odp",".zip",".tar",".gz",".7z",".rar",
        ".exe",".dmg",".pkg",".deb",".rpm",
    },

    # --- Queue / depth guards ---
    "MAX_QUEUE":   int(os.getenv("MAX_QUEUE", "50000")),

    # Paths to never fetch (not just hide from a report afterward) — one
    # per line via IGNORE_PATHS env var. Same rule as the webapp's Site
    # Comparison "Ignore paths" box: a bare entry excludes that path and
    # everything nested under it; a trailing "*" does a raw prefix match.
    "IGNORE_PATHS": [
        p.strip() for p in os.getenv("IGNORE_PATHS", "").split("\n") if p.strip()
    ],

    "TRAP_PATTERNS": set(
        os.getenv(
            "TRAP_PATTERNS",
            "/feed/,/rss/,/?s=,/?p=,/paged=,/?query=,/?q=,"
            "/media-library,/search?,search_api_page,"
            "/views/ajax,/jsonapi/",
        ).split(",")
    ),

    # 0 = follow all pagination; >0 blocks /page/N and ?page=N beyond this
    "MAX_PAGINATION_PAGE": int(os.getenv("MAX_PAGINATION_PAGE", "0")),

    # --- Rate limiting ---
    "RATE_LIMIT_BASE_WAIT": float(os.getenv("RATE_LIMIT_BASE_WAIT", "5.0")),
    "RATE_LIMIT_MAX_WAIT":  float(os.getenv("RATE_LIMIT_MAX_WAIT",  "120.0")),

    # --- Seeding ---
    "SEED_SITEMAP": os.getenv("SEED_SITEMAP", "true").lower() == "true",

    # --- Targeted scan ---
    # Specific page URLs to check instead of the full BFS crawl. Prefer
    # TARGET_PAGES_FILE (newline-separated) for anything but a small list -
    # a large CSV's worth of URLs joined into one TARGET_PAGES env var can
    # exceed the OS's combined argv+envp size limit (execve() then fails
    # with "OSError: Argument list too long", which has nothing to do with
    # the python3.12 executable path it names). TARGET_PAGES_FILE only ever
    # passes a short file path through the environment instead.
    "TARGET_PAGES": (
        [
            u.strip() for u in
            Path(os.environ["TARGET_PAGES_FILE"]).read_text(encoding="utf-8").splitlines()
            if u.strip()
        ] if os.getenv("TARGET_PAGES_FILE") else [
            u.strip() for u in os.getenv("TARGET_PAGES", "").split(",")
            if u.strip()
        ]
    ),

    # --- HTML report size control ---
    # Max rows embedded in the JSON data file served to the browser.
    # Non-200 rows are always included first; 200 OK rows fill the rest.
    # 0 = no limit (may crash browser for very large sites).
    "HTML_MAX_ROWS": int(os.getenv("HTML_MAX_ROWS", "100000")),

    # --- Image checking ---
    # On by default (client asked for this out of the box) - CHECK_IMAGES=
    # false opts back out to a link-only run when the extra runtime isn't
    # wanted.
    "CHECK_IMAGES": os.getenv("CHECK_IMAGES", "true").lower() == "true",

    # --- Link checking ---
    # On by default (this is a broken-link crawler). CHECK_LINKS=false skips
    # the HTTP status check + result row for each discovered <a href> (and,
    # for external links, skips them entirely) - but internal links are
    # still extracted and still drive site traversal either way, so an
    # images-only run (CHECK_LINKS=false, CHECK_IMAGES=true) still reaches
    # every page on the site. The page's own fetch status ("(page itself)"
    # rows) is unaffected by this flag - that's core crawl health info, not
    # a "link".
    "CHECK_LINKS": os.getenv("CHECK_LINKS", "true").lower() == "true",

    # --- Labels / metadata ---
    "SCAN_LABEL": os.getenv("SCAN_LABEL", ""),

    # --- Report upload ---
    # When set, every file in OUTPUT_DIR is uploaded to
    # s3://REPORT_BUCKET/<label>/<timestamp>/ after the reports are written.
    # Unset (the default) for local runs.
    "REPORT_BUCKET": os.getenv("REPORT_BUCKET", ""),

    # Google Drive upload is disabled (reports go to S3 only). To re-enable,
    # see the note in main() and uncomment these two keys.
    # "GOOGLE_DRIVE_CREDENTIALS": os.getenv("GOOGLE_DRIVE_CREDENTIALS", ""),
    # "GOOGLE_DRIVE_FOLDER_ID":   os.getenv("GOOGLE_DRIVE_FOLDER_ID", ""),
}

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("crawler")


# ─────────────────────────────────────────────────────────────
#  URL HELPERS
# ─────────────────────────────────────────────────────────────

def normalize(url: str, base: str = "") -> str | None:
    if not url:
        return None
    url = url.strip()
    if base:
        url = urljoin(base, url)
    try:
        p = urlparse(url)
    except Exception:
        return None
    scheme = p.scheme.lower()
    if scheme in CONFIG["SKIP_SCHEMES"]:
        return None
    if scheme not in ("http", "https"):
        return None

    # Strip noisy query params
    params  = parse_qs(p.query, keep_blank_values=True)
    cleaned = {k: v for k, v in params.items() if k not in CONFIG["STRIP_PARAMS"]}
    clean_query = urlencode(cleaned, doseq=True)

    # ── FIX 5: canonicalise www vs non-www to match BASE_URL exactly ──
    netloc      = p.netloc.lower()
    base_netloc = urlparse(CONFIG["BASE_URL"]).netloc.lower()
    bare_netloc = netloc.removeprefix("www.")
    bare_base   = base_netloc.removeprefix("www.")
    if bare_netloc == bare_base:
        netloc = base_netloc   # lock to BASE_URL's exact form

    clean = urlunparse((
        scheme,
        netloc,
        p.path.rstrip("/") or "/",
        p.params,
        clean_query,
        "",
    ))
    return clean


def _bare_domain(netloc: str) -> str:
    return netloc.lower().removeprefix("www.")


def is_same_domain(url: str, base: str) -> bool:
    return _bare_domain(urlparse(url).netloc) == _bare_domain(urlparse(base).netloc)


def should_skip_parse(url: str) -> bool:
    path     = urlparse(url).path.lower()
    all_skip = CONFIG["SKIP_PARSE_EXTS"] | CONFIG.get("DOC_EXTS", set())
    return any(path.endswith(ext) for ext in all_skip)


def is_doc_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    if "/files/" in path:
        return True
    return any(path.endswith(ext) for ext in CONFIG.get("DOC_EXTS", set()))


# ─────────────────────────────────────────────────────────────
#  EFFORT / CATEGORY CLASSIFICATION
# ─────────────────────────────────────────────────────────────

def effort_level(status) -> str:
    s = str(status)
    if s == "200":                        return "None"
    if s.startswith("3"):                 return "Low"
    if s in ("401", "403", "429"):        return "Low-Medium"
    if s in ("404", "410", "451"):        return "High"
    if s.startswith("5"):                 return "Medium"
    if "Timeout" in s or "SSL" in s:      return "Medium"
    if "Error" in s or "Connection" in s: return "High"
    return "Medium"


def status_category(status) -> str:
    s = str(status)
    if s == "200":          return "OK"
    if s.startswith("2"):   return "2xx Other"
    if s.startswith("3"):   return "Redirect"
    if s in ("404", "410"): return "Not Found"
    if s.startswith("4"):   return "4xx Client Error"
    if s.startswith("5"):   return "5xx Server Error"
    if "Timeout" in s:      return "Timeout"
    return "Error"


BOT_BLOCK_STATUSES = ("401", "403", "429")


def is_bot_blocked(row: dict) -> bool:
    """External link answered 401/403/429: the target site is refusing a
    scripted client (Cloudflare/Akamai challenge, rate limit), which says
    nothing about whether the link works in a browser. Internal 4xx are
    never counted here, those are the site's own problem."""
    return (row.get("link_type") == "External"
            and str(row.get("status", "")) in BOT_BLOCK_STATUSES)


# ─────────────────────────────────────────────────────────────
#  ASYNC HTTP HELPERS
# ─────────────────────────────────────────────────────────────

_rl_wait: float = 0.0
_rl_hits: int   = 0


async def _handle_429(url: str, retry_after: str = "") -> None:
    global _rl_wait, _rl_hits
    _rl_hits += 1
    if retry_after:
        try:
            wait = float(retry_after)
        except ValueError:
            wait = CONFIG.get("RATE_LIMIT_BASE_WAIT", 5.0)
    else:
        base  = CONFIG.get("RATE_LIMIT_BASE_WAIT", 5.0)
        limit = CONFIG.get("RATE_LIMIT_MAX_WAIT", 120.0)
        wait  = min(base * (2 ** (_rl_hits - 1)), limit)
    _rl_wait = wait
    log.warning("🚦 429 on %s — backing off %.0fs (hit #%d).", url[:70], wait, _rl_hits)
    await asyncio.sleep(wait)


def _reset_rl() -> None:
    global _rl_wait, _rl_hits
    if _rl_hits > 0:
        log.info("✅ Rate limit cleared — resuming normal speed.")
    _rl_wait = 0.0
    _rl_hits = 0


async def fetch_page_html(
    session: aiohttp.ClientSession, url: str
) -> tuple:
    headers = {"User-Agent": CONFIG["USER_AGENT"]}
    timeout = aiohttp.ClientTimeout(total=CONFIG["TIMEOUT"])
    for attempt in range(1, 4):
        try:
            async with session.get(
                url, headers=headers, timeout=timeout,
                allow_redirects=True, ssl=False,
            ) as resp:
                if resp.status == 429:
                    await _handle_429(url, resp.headers.get("Retry-After", ""))
                    continue
                _reset_rl()
                try:
                    html = await resp.text(errors="ignore")
                except Exception:
                    html = ""
                return resp.status, html, str(resp.url)
        except asyncio.TimeoutError:
            if attempt < 3:
                await asyncio.sleep(attempt * 2)
                continue
            return "Timeout", "", url
        except aiohttp.ClientSSLError:
            return "SSL Error", "", url
        except aiohttp.ClientConnectorError:
            return "Connection Error", "", url
        except Exception as e:
            return f"Error: {type(e).__name__}", "", url
    return "Timeout", "", url


async def check_link_status(
    session: aiohttp.ClientSession, url: str, link_cache: dict
) -> tuple:
    if url in link_cache:
        return link_cache[url]
    headers = {"User-Agent": CONFIG["USER_AGENT"]}
    timeout = aiohttp.ClientTimeout(total=CONFIG["TIMEOUT"])
    t0     = time.monotonic()
    result = ("Timeout", url, -1)
    for _attempt in range(3):
        try:
            async with session.head(
                url, headers=headers, timeout=timeout,
                allow_redirects=True, ssl=False,
            ) as resp:
                load_ms = round((time.monotonic() - t0) * 1000)
                if resp.status == 429:
                    await _handle_429(url, resp.headers.get("Retry-After", ""))
                    t0 = time.monotonic()
                    continue
                if resp.status in (405, 403):
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=resp.status
                    )
                _reset_rl()
                result = (resp.status, str(resp.url), load_ms)
                break
        except aiohttp.ClientResponseError:
            t0 = time.monotonic()
            try:
                async with session.get(
                    url, headers=headers, timeout=timeout,
                    allow_redirects=True, ssl=False,
                ) as resp:
                    load_ms = round((time.monotonic() - t0) * 1000)
                    if resp.status == 429:
                        await _handle_429(url, resp.headers.get("Retry-After", ""))
                        t0 = time.monotonic()
                        continue
                    _reset_rl()
                    result = (resp.status, str(resp.url), load_ms)
                    break
            except asyncio.TimeoutError:
                result = ("Timeout", url, -1)
                break
            except Exception as e:
                result = (f"Error: {type(e).__name__}", url, -1)
                break
        except asyncio.TimeoutError:
            result = ("Timeout", url, -1)
            break
        except aiohttp.ClientSSLError:
            result = ("SSL Error", url, -1)
            break
        except aiohttp.ClientConnectorError:
            result = ("Connection Error", url, -1)
            break
        except Exception as e:
            result = (f"Error: {type(e).__name__}", url, -1)
            break
    link_cache[url] = result
    return result


# ─────────────────────────────────────────────────────────────
#  HTML PARSER — plain fallback (no BeautifulSoup)
# ─────────────────────────────────────────────────────────────

def extract_links(html: str, page_url: str) -> list[tuple[str, str]]:
    """Flat link extractor — used as fallback when BS4 unavailable."""
    from html.parser import HTMLParser

    class _LP(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links: list[tuple[str, str]] = []
            self._in_a       = False
            self._href: str | None = None
            self._buf:  list[str]  = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self._in_a  = True
                d           = dict(attrs)
                self._href  = normalize(d.get("href", "").strip(), page_url)
                self._buf   = []

        def handle_data(self, data):
            if self._in_a:
                self._buf.append(data.strip())

        def handle_endtag(self, tag):
            if tag == "a" and self._in_a:
                if self._href:
                    text = " ".join(t for t in self._buf if t)[:200]
                    self.links.append((self._href, text))
                self._in_a  = False
                self._href  = None
                self._buf   = []

    p = _LP()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.links


# ─────────────────────────────────────────────────────────────
#  FIX 1–4: STRUCTURAL DEDUPLICATION
#  Header / footer / nav / sidebar links are captured ONCE
#  (from the home page) and then suppressed on subsequent pages.
#  Brand-new nav signatures (hidden / secondary menus) are
#  captured the first time they appear anywhere.
# ─────────────────────────────────────────────────────────────

_global_structural_links: set[str] = set()
_known_nav_signatures:    set[str] = set()


def _reset_structural_cache() -> None:
    _global_structural_links.clear()
    _known_nav_signatures.clear()


def _nav_signature(tag) -> str:
    tag_id    = tag.get("id", "")
    tag_class = " ".join(tag.get("class", []))
    return f"{tag_id}|{tag_class}"


# CSS class fragments that identify structural zones
_HDR_CLASSES  = ("site-header","page-header","masthead","top-bar","header-wrap",
                  "global-header","navbar","nav-bar")
_FTR_CLASSES  = ("site-footer","page-footer","footer-wrap","bottom-bar",
                  "global-footer","footer-inner")
_SIDE_CLASSES = ("sidebar","side-nav","sidenav","widget-area","offcanvas",
                  "side-panel","lateral-nav")
_NAV_CLASSES  = ("main-nav","primary-nav","site-nav","main-menu","primary-menu",
                  "top-menu","global-nav","nav-primary","navigation-main",
                  "mega-menu","dropdown-menu")


def _classify_zone(a_tag) -> str:
    """Walk the tag's ancestors and return a zone label."""
    for parent in a_tag.parents:
        name = getattr(parent, "name", None)
        if not name or name in ("[document]", "html", "body"):
            continue
        if name == "header":
            return "header"
        if name == "footer":
            return "footer"
        if name == "aside":
            return "aside"
        if name == "nav":
            sig = _nav_signature(parent)
            return f"nav:{sig}"
        if not hasattr(parent, "get"):
            continue
        cls = " ".join(parent.get("class", [])).lower()
        if any(x in cls for x in _HDR_CLASSES):
            return "header"
        if any(x in cls for x in _FTR_CLASSES):
            return "footer"
        if any(x in cls for x in _SIDE_CLASSES):
            return f"aside:{cls[:80]}"
        if any(x in cls for x in _NAV_CLASSES):
            return f"nav:class:{cls[:80]}"
    return "content"


def extract_links_zoned(
    html: str,
    page_url: str,
    is_home: bool,
) -> list[tuple[str, str]]:
    """
    Zone-aware link extractor.

    Home page   → capture all links and register structural ones globally.
    Other pages → suppress structural links already seen; capture new
                  nav signatures once (hidden / secondary menus).

    Falls back to plain extract_links() if BeautifulSoup is unavailable.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return extract_links(html, page_url)

    soup    = BeautifulSoup(html, "lxml")
    results: list[tuple[str, str]] = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        text = a.get_text(separator=" ", strip=True)[:200]
        norm = normalize(href, page_url)
        if not norm:
            continue

        zone         = _classify_zone(a)
        is_structural = zone != "content"

        if not is_structural:
            results.append((norm, text))
            continue

        if is_home:
            # Capture everything on the home page; register all structural URLs
            _global_structural_links.add(norm)
            if zone.startswith("nav:"):
                _known_nav_signatures.add(zone)
            results.append((norm, text))

        else:
            # New nav signature discovered (e.g. page-specific hidden menu) → capture once
            if zone.startswith("nav:") and zone not in _known_nav_signatures:
                _known_nav_signatures.add(zone)
                _global_structural_links.add(norm)
                log.info(
                    "🧭  New nav zone on %s → %s",
                    page_url[:70], zone[:80],
                )
                results.append((norm, text))

            elif norm not in _global_structural_links:
                # Structural URL never seen before → capture once
                _global_structural_links.add(norm)
                results.append((norm, text))
            # else: already catalogued structural link → skip

    return results


def extract_images(html: str, page_url: str) -> list[tuple[str, str]]:
    """Flat image extractor - used as fallback when BS4 unavailable."""
    from html.parser import HTMLParser

    class _IP(HTMLParser):
        def __init__(self):
            super().__init__()
            self.images: list[tuple[str, str]] = []

        def handle_starttag(self, tag, attrs):
            if tag != "img":
                return
            d   = dict(attrs)
            src = normalize(d.get("src", "").strip(), page_url)
            if src:
                self.images.append((src, d.get("alt", "").strip()[:200]))

    p = _IP()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.images


def _srcset_urls(srcset: str, page_url: str) -> list[str]:
    urls: list[str] = []
    for candidate in srcset.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        url  = candidate.split()[0]  # drop trailing "1x" / "640w" descriptor
        norm = normalize(url, page_url)
        if norm and norm not in urls:
            urls.append(norm)
    return urls


def extract_images_zoned(
    html: str,
    page_url: str,
    is_home: bool,
) -> list[tuple[str, str]]:
    """
    Zone-aware image extractor - mirrors extract_links_zoned(): same zone
    classification, same _global_structural_links / _known_nav_signatures
    caches, so a URL captured once as a structural link also suppresses it
    as a structural image (and vice versa).

    Falls back to plain extract_images() if BeautifulSoup is unavailable.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return extract_images(html, page_url)

    soup    = BeautifulSoup(html, "lxml")
    results: list[tuple[str, str]] = []

    for img in soup.find_all("img"):
        alt = img.get("alt", "").strip()[:200]

        candidates: list[str] = []
        src = img.get("src", "").strip()
        if src:
            norm = normalize(src, page_url)
            if norm:
                candidates.append(norm)
        srcset = img.get("srcset", "").strip()
        if srcset:
            for norm in _srcset_urls(srcset, page_url):
                if norm not in candidates:
                    candidates.append(norm)

        zone          = _classify_zone(img)
        is_structural = zone != "content"

        for norm in candidates:
            if not is_structural:
                results.append((norm, alt))
                continue

            if is_home:
                _global_structural_links.add(norm)
                if zone.startswith("nav:"):
                    _known_nav_signatures.add(zone)
                results.append((norm, alt))
            else:
                if zone.startswith("nav:") and zone not in _known_nav_signatures:
                    _known_nav_signatures.add(zone)
                    _global_structural_links.add(norm)
                    results.append((norm, alt))
                elif norm not in _global_structural_links:
                    _global_structural_links.add(norm)
                    results.append((norm, alt))
                # else: already catalogued structural URL -> skip

    return results


# ─────────────────────────────────────────────────────────────
#  TARGETED CRAWL  — scan a specific list of pages only
# ─────────────────────────────────────────────────────────────

async def targeted_crawl(target_urls: list[str]) -> list[dict]:
    base_url   = CONFIG["BASE_URL"].rstrip("/")
    sem        = asyncio.Semaphore(CONFIG["CONCURRENCY"])
    link_cache: dict       = {}
    results:    list[dict] = []

    log.info("TARGETED MODE — scanning %d specific page(s):", len(target_urls))
    for u in target_urls:
        log.info("  → %s", u)

    live_dir = Path(CONFIG["OUTPUT_DIR"])
    live_dir.mkdir(parents=True, exist_ok=True)
    status_path = live_dir / "live_status.json"
    jsonl_path  = live_dir / "live_results.jsonl"
    try:
        jsonl_path.unlink()
    except FileNotFoundError:
        pass
    _flushed = 0
    _scanned: set[str] = set()

    def _write_status(
        phase: str, current_batch: list[str] | None = None, outcome: str | None = None,
    ) -> None:
        payload = {
            "phase":         phase,
            "base_url":      CONFIG["BASE_URL"],
            "visited":       len(_scanned),
            "queue":         max(0, len(target_urls) - len(_scanned)),
            "results":       len(results),
            "current_batch": current_batch or [],
            "updated":       time.time(),
        }
        if outcome is not None:
            payload["outcome"] = outcome
        if phase == "done":
            payload["ended_display"] = now_est().strftime("%Y-%m-%d %H:%M:%S %Z")
        tmp = status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(status_path)

    def _flush_jsonl() -> None:
        nonlocal _flushed
        new_rows = results[_flushed:]
        if not new_rows:
            return
        with open(jsonl_path, "a", encoding="utf-8") as jf:
            for r in new_rows:
                jf.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
        _flushed = len(results)

    _write_status("crawling", list(target_urls))

    connector = aiohttp.TCPConnector(
        limit=CONFIG["CONCURRENCY"] + 5,
        ssl=False,
        force_close=False,
        enable_cleanup_closed=True,
    )

    async with aiohttp.ClientSession(connector=connector) as session:

        async def scan_page(page_url: str):
            log.info("Scanning page: %s", page_url)
            async with sem:
                status, html, final_url = await fetch_page_html(session, page_url)

            # Always record the page's own fetch status (not just on
            # failure) so page-level status is available for tools that
            # need it (e.g. live vs go-live comparison), even when 200.
            results.append({
                "page_url":  page_url,
                "link_url":  page_url,
                "link_text": "(page itself)",
                "link_type": "Page",
                "status":    status,
                "final_url": final_url,
                "load_ms":   -1,
                "depth":     0,
                "effort":    effort_level(status),
                "category":  status_category(status),
                "timestamp": now_est().strftime("%Y-%m-%d %H:%M:%S"),
            })
            _scanned.add(page_url)
            _flush_jsonl()
            _write_status("crawling")

            if str(status) != "200":
                return

            if should_skip_parse(page_url):
                return

            is_home     = (normalize(page_url) == normalize(base_url))
            page_links  = extract_links_zoned(html, page_url, is_home=is_home)
            page_images = (
                extract_images_zoned(html, page_url, is_home=is_home)
                if CONFIG["CHECK_IMAGES"] else []
            )

            if not page_links and not page_images:
                log.info("  No links or images found on %s", page_url)
                return
            log.info(
                "  Found %d links, %d images on %s",
                len(page_links), len(page_images), page_url,
            )

            async def check_one(link_url: str, link_text: str, resource_type: str = "Link"):
                if resource_type == "Link" and not CONFIG["CHECK_LINKS"]:
                    return
                async with sem:
                    lnk_status, lnk_final, lnk_load_ms = await check_link_status(
                        session, link_url, link_cache
                    )
                link_type = (
                    "Internal" if is_same_domain(link_url, base_url) else "External"
                )
                results.append({
                    "page_url":      page_url,
                    "link_url":      link_url,
                    "link_text":     link_text,
                    "link_type":     link_type,
                    "status":        lnk_status,
                    "final_url":     lnk_final,
                    "load_ms":       lnk_load_ms,
                    "depth":         0,
                    "effort":        effort_level(lnk_status),
                    "category":      status_category(lnk_status),
                    "timestamp":     now_est().strftime("%Y-%m-%d %H:%M:%S"),
                    "resource_type": resource_type,
                })

            await asyncio.gather(
                *[check_one(lu, lt) for lu, lt in page_links],
                *[check_one(iu, it, "Image") for iu, it in page_images],
            )

        await asyncio.gather(*[scan_page(u) for u in target_urls])

    _flush_jsonl()
    _write_status("done", outcome="completed")

    log.info("Targeted scan complete. %d link records collected.", len(results))
    return results


# ─────────────────────────────────────────────────────────────
#  TRAP / SKIP HELPERS
# ─────────────────────────────────────────────────────────────

_skipped: dict[str, list] = {}


def _record_skip(url: str, reason: str, pattern: str) -> None:
    bucket = _skipped.setdefault(reason, [])
    if len(bucket) < 500:
        bucket.append((url, pattern))


def _get_matching_pattern(url: str) -> str:
    lower = url.lower()
    for pat in CONFIG["TRAP_PATTERNS"]:
        if pat.strip() and pat.strip() in lower:
            return pat.strip()
    return ""


def _reset_skip_tracking() -> None:
    _skipped.clear()


def is_ignored_path(url: str) -> bool:
    """Same ignore-path rule as the webapp's Site Comparison tab's
    _is_ignored(): a bare entry excludes that path and everything nested
    under it (but not a same-prefix sibling like "/archives1"); a
    trailing "*" does a raw prefix match instead ("/archives*" also
    matches "/archives1"). Checked at enqueue time so ignored paths are
    never fetched at all, not just hidden from a report afterward."""
    patterns = CONFIG.get("IGNORE_PATHS") or []
    if not patterns:
        return False
    path = urlparse(url).path.strip().rstrip("/").lower() or "/"
    for raw in patterns:
        wildcard = raw.endswith("*")
        entry = raw[:-1] if wildcard else raw
        entry = entry.strip().rstrip("/").lower()
        if not entry:
            continue
        if not entry.startswith("/"):
            entry = "/" + entry
        if wildcard:
            if path.startswith(entry):
                return True
        elif path == entry or path.startswith(entry + "/"):
            return True
    return False


def is_trap_url(url: str) -> bool:
    lower = url.lower()
    if any(p.strip() and p.strip() in lower for p in CONFIG["TRAP_PATTERNS"]):
        return True
    max_pag = CONFIG.get("MAX_PAGINATION_PAGE", 0)
    if max_pag > 0:
        m = re.search(r"/page/(\d+)", lower)
        if m and int(m.group(1)) > max_pag:
            return True
        m2 = re.search(r"[?&]page=(\d+)", lower)
        if m2 and int(m2.group(1)) > max_pag:
            return True
    return False


def safe_enqueue(queue: deque, seen: set, url: str, depth: int) -> bool:
    if url in seen:
        return False
    if depth > CONFIG["MAX_DEPTH"]:
        _record_skip(url, "depth_exceeded", f"depth={depth}>max={CONFIG['MAX_DEPTH']}")
        return False
    if len(queue) >= CONFIG["MAX_QUEUE"]:
        seen.add(url)
        if not getattr(safe_enqueue, "_cap_warned", False):
            log.warning(
                "⚠️  MAX_QUEUE cap (%d) reached — new URLs dropped. "
                "Increase MAX_QUEUE or use dropped_pages.txt for a follow-up scan.",
                CONFIG["MAX_QUEUE"],
            )
            safe_enqueue._cap_warned = True
        if not hasattr(safe_enqueue, "_dropped_urls"):
            safe_enqueue._dropped_urls = []
        safe_enqueue._dropped_urls.append(url)
        return False
    if is_trap_url(url):
        _record_skip(url, "trap", _get_matching_pattern(url))
        return False
    if is_ignored_path(url):
        _record_skip(url, "ignored_path", "")
        return False
    if should_skip_parse(url):
        _record_skip(url, "binary_ext", "")
        return False
    if is_doc_url(url):
        _record_skip(url, "doc_ext", "")
        return False
    seen.add(url)
    queue.append((url, depth))
    return True


# ─────────────────────────────────────────────────────────────
#  DNS / IP RESOLUTION
# ─────────────────────────────────────────────────────────────

_dns_cache: dict[str, str] = {}


def _resolve_sync(hostname: str) -> str:
    try:
        infos = socket.getaddrinfo(hostname, None)
        ipv4  = [i[4][0] for i in infos if i[0] == socket.AF_INET]
        ipv6  = [i[4][0] for i in infos if i[0] == socket.AF_INET6]
        return ipv4[0] if ipv4 else (ipv6[0] if ipv6 else "N/A")
    except Exception:
        return "N/A"


async def resolve_ip(hostname: str) -> str:
    if hostname in _dns_cache:
        return _dns_cache[hostname]
    loop = asyncio.get_event_loop()
    ip   = await loop.run_in_executor(None, _resolve_sync, hostname)
    _dns_cache[hostname] = ip
    return ip


async def enrich_with_ips(results: list[dict]) -> None:
    hostnames: set[str] = set()
    for r in results:
        h1 = urlparse(r.get("page_url", "")).hostname or ""
        h2 = urlparse(r.get("link_url", "")).hostname or ""
        if h1:
            hostnames.add(h1)
        if h2:
            hostnames.add(h2)
    hostnames.discard("")
    log.info("🔍 Resolving IPs for %d unique hostnames…", len(hostnames))
    await asyncio.gather(*[resolve_ip(h) for h in hostnames])
    resolved = sum(1 for h in hostnames if _dns_cache.get(h, "N/A") != "N/A")
    log.info("🔍 IP resolution: %d/%d resolved", resolved, len(hostnames))
    for r in results:
        ph = urlparse(r.get("page_url", "")).hostname or ""
        lh = urlparse(r.get("link_url", "")).hostname or ""
        r["page_ip"] = _dns_cache.get(ph, "N/A")
        r["link_ip"] = _dns_cache.get(lh, "N/A")


# ─────────────────────────────────────────────────────────────
#  SITEMAP SEEDER
# ─────────────────────────────────────────────────────────────

async def _seed_from_sitemap(
    base_url: str, queue: deque, seen: set, session: aiohttp.ClientSession
) -> int:
    added   = 0
    sm_q: list[str] = []
    visited: set[str] = set()
    headers = {"User-Agent": CONFIG["USER_AGENT"]}
    timeout = aiohttp.ClientTimeout(total=20)
    base    = base_url.rstrip("/")

    try:
        async with session.get(
            f"{base}/robots.txt", headers=headers, timeout=timeout, ssl=False
        ) as resp:
            if resp.status == 200:
                for line in (await resp.text(errors="ignore")).splitlines():
                    if line.strip().lower().startswith("sitemap:"):
                        sm_url = line.split(":", 1)[1].strip()
                        if sm_url and sm_url not in visited:
                            sm_q.append(sm_url)
                            log.info("🗺  Sitemap from robots.txt: %s", sm_url)
    except Exception as e:
        log.debug("robots.txt sitemap check failed: %s", e)

    if not sm_q:
        for path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap/",
                     "/sitemaps/1", "/sitemap-index.xml"]:
            sm_q.append(base + path)

    while sm_q:
        sm_url = sm_q.pop(0)
        if sm_url in visited:
            continue
        visited.add(sm_url)
        try:
            async with session.get(
                sm_url, headers=headers, timeout=timeout, ssl=False
            ) as resp:
                if resp.status != 200:
                    continue
                ct = resp.headers.get("Content-Type", "")
                if "html" in ct.lower():
                    continue
                text = await resp.text(errors="ignore")
        except Exception as e:
            log.debug("Sitemap fetch failed %s: %s", sm_url, e)
            continue

        for loc in re.findall(
            r"<sitemap>\s*<loc>\s*(.*?)\s*</loc>", text, re.DOTALL
        ):
            if loc.strip() not in visited:
                sm_q.append(loc.strip())

        for loc in re.findall(
            r"<url>\s*<loc>\s*(.*?)\s*</loc>", text, re.DOTALL
        ):
            loc  = loc.strip()
            norm = normalize(loc)
            if not norm or not is_same_domain(norm, base_url):
                continue
            if norm in seen or is_doc_url(norm):
                continue
            # Sitemap seeding bypasses safe_enqueue() entirely (it writes
            # straight to seen/queue), so without this check an ignored
            # path listed in sitemap.xml is fetched anyway even though
            # every link-discovered URL to the same path is correctly
            # skipped by safe_enqueue()'s own is_ignored_path() check.
            if is_ignored_path(norm):
                continue
            seen.add(norm)
            queue.append((norm, 0))
            added += 1

    if added:
        log.info("🗺  Sitemap seeding: %d URLs added to queue", added)
    else:
        log.warning("🗺  No sitemap found — relying on link discovery only")
    return added


def load_robots(base_url: str) -> RobotFileParser:
    rp = RobotFileParser()
    try:
        rp.set_url(base_url.rstrip("/") + "/robots.txt")
        rp.read()
    except Exception:
        # stdlib RobotFileParser fails CLOSED (blocks everything) when
        # robots.txt was never successfully read — e.g. DNS failure on an
        # unreachable staging/go-live host. That would silently zero out
        # the crawl. Fail OPEN instead: no robots.txt info means no
        # restriction we can enforce.
        log.warning(
            "robots.txt fetch failed for %s — proceeding without robots restrictions.",
            base_url,
        )
        rp.allow_all = True
    return rp


def robots_allow(rp: RobotFileParser, url: str) -> bool:
    if not CONFIG["RESPECT_ROBOTS"]:
        return True
    try:
        return rp.can_fetch(CONFIG["USER_AGENT"], url)
    except Exception:
        return True


# ─────────────────────────────────────────────────────────────
#  MAIN BFS CRAWL
# ─────────────────────────────────────────────────────────────

async def crawl() -> tuple[list[dict], list, dict]:
    base_url = CONFIG["BASE_URL"].rstrip("/")
    sem      = asyncio.Semaphore(CONFIG["CONCURRENCY"])

    # Reset structural dedup cache for clean re-runs
    _reset_structural_cache()

    _shutdown = asyncio.Event()

    def _on_signal(sig_name: str):
        if not _shutdown.is_set():
            log.warning(
                "⚠️  %s — finishing batch then writing partial report…", sig_name
            )
            _shutdown.set()

    # Signal handling: Unix uses add_signal_handler; Windows uses signal.signal
    _loop = asyncio.get_event_loop()
    if sys.platform != "win32":
        for _s in (signal.SIGINT, signal.SIGTERM):
            try:
                _loop.add_signal_handler(_s, lambda n=_s.name: _on_signal(n))
            except (NotImplementedError, ValueError, AttributeError):
                pass
    else:
        def _win_handler(sig, frame):
            _on_signal("SIGINT")
        signal.signal(signal.SIGINT, _win_handler)

    visited_pages:   set[str]   = set()
    completed_pages: set[str]   = set()
    seen:            set[str]   = set()
    link_cache:      dict       = {}
    results:         list[dict] = []
    trap_skipped:    int        = 0

    rp    = load_robots(base_url)
    start = normalize(base_url)
    if not start:
        log.error("BASE_URL '%s' is invalid.", base_url)
        return [], [], {}

    queue: deque[tuple[str, int]] = deque()
    seen.add(start)
    queue.append((start, 0))

    # ── Live status / incremental results streaming ──
    # Lets an external UI (e.g. the Go-Live Comparison webapp tab) tail
    # progress and partial results while the crawl is still running,
    # instead of waiting for the final CSV/Excel/HTML at the end.
    live_dir = Path(CONFIG["OUTPUT_DIR"])
    live_dir.mkdir(parents=True, exist_ok=True)
    status_path = live_dir / "live_status.json"
    jsonl_path  = live_dir / "live_results.jsonl"
    try:
        jsonl_path.unlink()
    except FileNotFoundError:
        pass
    _flushed = 0

    def _write_status(
        phase: str, current_batch: list[str] | None = None, outcome: str | None = None,
    ) -> None:
        payload = {
            "phase":         phase,
            "base_url":      CONFIG["BASE_URL"],
            "visited":       len(visited_pages),
            "queue":         len(queue),
            "results":       len(results),
            "current_batch": current_batch or [],
            "updated":       time.time(),
        }
        if outcome is not None:
            # "completed" (queue fully exhausted), "max_pages_cap" (hit the
            # MAX_PAGES limit), or "stopped" (Stop button / SIGTERM/SIGINT)
            # — lets a UI show whether this was a full crawl or a partial
            # one, instead of only ever seeing phase="done" either way.
            payload["outcome"] = outcome
        if phase == "done":
            payload["ended_display"] = now_est().strftime("%Y-%m-%d %H:%M:%S %Z")
        tmp = status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(status_path)

    def _flush_jsonl() -> None:
        nonlocal _flushed
        new_rows = results[_flushed:]
        if not new_rows:
            return
        with open(jsonl_path, "a", encoding="utf-8") as jf:
            for r in new_rows:
                jf.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
        _flushed = len(results)

    connector = aiohttp.TCPConnector(
        limit=CONFIG["CONCURRENCY"] + 5,
        ssl=False,
        force_close=False,
        enable_cleanup_closed=True,
    )

    async with aiohttp.ClientSession(connector=connector) as session:

        if CONFIG.get("SEED_SITEMAP"):
            await _seed_from_sitemap(base_url, queue, seen, session)

        while queue:
            if _shutdown.is_set():
                log.warning(
                    "Shutdown requested — writing partial report "
                    "(%d results collected).", len(results)
                )
                break

            if CONFIG["MAX_PAGES"] > 0 and len(visited_pages) >= CONFIG["MAX_PAGES"]:
                log.warning("MAX_PAGES cap (%d) reached — crawl complete.",
                            CONFIG["MAX_PAGES"])
                break

            batch: list[tuple[str, int]] = []
            while queue and len(batch) < CONFIG["CONCURRENCY"]:
                # Checked every iteration (not just at the top of the outer
                # while) so a low page cap stops adding pages the instant
                # it's hit, instead of finishing out the rest of this batch.
                if (CONFIG["MAX_PAGES"] > 0
                        and len(visited_pages) >= CONFIG["MAX_PAGES"]):
                    break
                url, depth = queue.popleft()
                if url in visited_pages:
                    continue
                if depth > CONFIG["MAX_DEPTH"]:
                    continue
                if not robots_allow(rp, url):
                    log.debug("robots.txt disallows: %s", url)
                    continue
                visited_pages.add(url)
                batch.append((url, depth))

            if not batch:
                break

            _write_status("crawling", [u for u, _ in batch])

            log.info(
                "Crawling batch %d pages | visited: %d | queue: %d | trap-skipped: %d",
                len(batch), len(visited_pages), len(queue), trap_skipped,
            )

            async def process_page(page_url: str, depth: int):
                nonlocal trap_skipped

                async with sem:
                    status, html, final_url = await fetch_page_html(
                        session, page_url
                    )

                # Always record the page's own fetch status (not just on
                # failure) so page-level status is available for tools that
                # need it (e.g. live vs go-live comparison), even when 200.
                results.append({
                    "page_url":  page_url,
                    "link_url":  page_url,
                    "link_text": "(page itself)",
                    "link_type": "Page",
                    "status":    status,
                    "final_url": final_url,
                    "load_ms":   -1,
                    "depth":     depth,
                    "effort":    effort_level(status),
                    "category":  status_category(status),
                    "timestamp": now_est().strftime("%Y-%m-%d %H:%M:%S"),
                })

                if str(status) != "200":
                    return

                if should_skip_parse(page_url):
                    return

                is_home     = (normalize(page_url) == normalize(base_url))
                page_links  = extract_links_zoned(html, page_url, is_home=is_home)
                page_images = (
                    extract_images_zoned(html, page_url, is_home=is_home)
                    if CONFIG["CHECK_IMAGES"] else []
                )
                if not page_links and not page_images:
                    return

                async def check_one(link_url: str, link_text: str, resource_type: str = "Link"):
                    nonlocal trap_skipped

                    # CHECK_LINKS=false skips the HTTP status check + result
                    # row for links (external links are dropped outright -
                    # they're dead ends for the crawl either way), but
                    # internal links still have to drive traversal, so they
                    # still get enqueued here without a status pre-check.
                    # The page's real status is discovered anyway once it's
                    # actually fetched (recorded as its own "(page itself)"
                    # row), so nothing about site coverage changes.
                    if resource_type == "Link" and not CONFIG["CHECK_LINKS"]:
                        if not is_same_domain(link_url, base_url):
                            return
                        if (CONFIG["MAX_PAGES"] > 0
                                and len(visited_pages) >= CONFIG["MAX_PAGES"]):
                            return
                        if is_trap_url(link_url):
                            trap_skipped += 1
                            _record_skip(link_url, "trap", _get_matching_pattern(link_url))
                            return
                        safe_enqueue(queue, seen, link_url, depth + 1)
                        return

                    async with sem:
                        lnk_status, lnk_final, lnk_ms = await check_link_status(
                            session, link_url, link_cache
                        )
                    link_type = (
                        "Internal"
                        if is_same_domain(link_url, base_url)
                        else "External"
                    )
                    results.append({
                        "page_url":      page_url,
                        "link_url":      link_url,
                        "link_text":     link_text,
                        "link_type":     link_type,
                        "status":        lnk_status,
                        "final_url":     lnk_final,
                        "load_ms":       lnk_ms,
                        "depth":         depth,
                        "effort":        effort_level(lnk_status),
                        "category":      status_category(lnk_status),
                        "timestamp":     now_est().strftime("%Y-%m-%d %H:%M:%S"),
                        "resource_type": resource_type,
                    })

                    # Images are leaf resources - never re-enqueued, never
                    # trap-checked (that logic exists purely to keep the
                    # crawl queue from growing, which images never do).
                    if resource_type != "Link":
                        return
                    if link_type != "Internal":
                        return
                    if (CONFIG["MAX_PAGES"] > 0
                            and len(visited_pages) >= CONFIG["MAX_PAGES"]):
                        return
                    if str(lnk_status) in ("404", "410", "451"):
                        return
                    if is_trap_url(link_url):
                        trap_skipped += 1
                        _record_skip(link_url, "trap", _get_matching_pattern(link_url))
                        return
                    safe_enqueue(queue, seen, link_url, depth + 1)

                await asyncio.gather(
                    *[check_one(lu, lt) for lu, lt in page_links],
                    *[check_one(iu, it, "Image") for iu, it in page_images],
                )

            await asyncio.gather(*[process_page(u, d) for u, d in batch])

            for page_url, _ in batch:
                completed_pages.add(page_url)

            _flush_jsonl()
            _write_status("crawling")

            _delay = max(CONFIG["POLITE_DELAY"], _rl_wait)
            if _delay > 0:
                await asyncio.sleep(_delay)

    # Determined from final state rather than tracked through scattered
    # break points above — the cap can also be what silently starved the
    # queue dry (e.g. check_one() suppressing further enqueues once the
    # cap is hit), which doesn't pass through either of the loop's own
    # cap checks but is still "capped", not "completed".
    if _shutdown.is_set():
        stop_reason = "stopped"
    elif CONFIG["MAX_PAGES"] > 0 and len(visited_pages) >= CONFIG["MAX_PAGES"]:
        stop_reason = "max_pages_cap"
    else:
        stop_reason = "completed"

    _flush_jsonl()
    _write_status("done", outcome=stop_reason)

    dropped_urls = getattr(safe_enqueue, "_dropped_urls", [])
    dropped      = len(dropped_urls)
    if dropped:
        log.warning(
            "⚠️  MAX_QUEUE summary: %d URLs dropped (cap=%d). "
            "See dropped_pages.txt in the reports folder.",
            dropped, CONFIG["MAX_QUEUE"],
        )
    safe_enqueue._cap_warned   = False
    safe_enqueue._dropped_urls = []

    if _skipped:
        log.info("── Skipped URL summary ──────────────────────────────────")
        for reason, entries in sorted(_skipped.items(), key=lambda x: -len(x[1])):
            pat_counts = Counter(pat for _, pat in entries)
            top        = pat_counts.most_common(5)
            examples   = [url for url, _ in entries[:3]]
            log.info(
                "  %-20s %5d URLs  top patterns: %s",
                reason, len(entries),
                ", ".join(f"'{p}'({n})" for p, n in top) or "n/a",
            )
            for ex in examples:
                log.info("    e.g. %s", ex[:100])
        log.info("─────────────────────────────────────────────────────────")

    skipped_snapshot = {r: list(e) for r, e in _skipped.items()}
    _reset_skip_tracking()

    log.info(
        "Crawl complete. visited=%d  completed=%d  results=%d  "
        "trap-skipped=%d  queue-dropped=%d  queue-remaining=%d",
        len(visited_pages), len(completed_pages),
        len(results), trap_skipped, dropped, len(queue),
    )
    return results, dropped_urls, skipped_snapshot


# ─────────────────────────────────────────────────────────────
#  CSV OUTPUT
# ─────────────────────────────────────────────────────────────

FIELDS = [
    "page_url","link_url","link_text","link_type",
    "status","final_url","load_ms","depth","effort","category",
    "page_ip","link_ip","timestamp","resource_type",
]


def write_csv(results: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    log.info("CSV written → %s  (%d rows)", path, len(results))


# ─────────────────────────────────────────────────────────────
#  EXCEL OUTPUT  (openpyxl) — includes FIX 6: Pivot sheet
# ─────────────────────────────────────────────────────────────

def write_excel(
    results: list[dict], path: str,
    start_dt: str = None, end_dt: str = None, duration_str: str = None,
) -> None:
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        log.warning("openpyxl not installed — skipping Excel output. "
                    "Install with: pip install openpyxl")
        return

    STATUS_FILLS = {
        "ok":        PatternFill("solid", fgColor="D4EDDA"),
        "redirect":  PatternFill("solid", fgColor="FFF3CD"),
        "broken":    PatternFill("solid", fgColor="F8D7DA"),
        "warn":      PatternFill("solid", fgColor="FFE5CC"),
        "servererr": PatternFill("solid", fgColor="E2D9F3"),
        "unkn":      PatternFill("solid", fgColor="E9ECEF"),
    }
    HDR_FILL  = PatternFill("solid", fgColor="1F4E79")
    HDR_FONT  = Font(bold=True, color="FFFFFF", size=11)
    BOLD      = Font(bold=True)
    thin      = Side(border_style="thin", color="CCCCCC")
    BORDER    = Border(left=thin, right=thin, top=thin, bottom=thin)

    def rc(status: str) -> str:
        s = str(status)
        if s == "200":         return "ok"
        if s.startswith("3"):  return "redirect"
        if s in ("404","410"): return "broken"
        if s.startswith("4"):  return "warn"
        if s.startswith("5"):  return "servererr"
        return "unkn"

    EXCEL_MAX   = 1_048_576
    headers     = ["Parent URL","Link URL","Anchor Text","Type","Status",
                   "Category","Depth","Effort","Load Time (ms)","Timestamp",
                   "Parent IP","Link IP"]
    col_widths  = [70, 70, 35, 12, 12, 18, 8, 14, 16, 24, 16, 16]

    wb = openpyxl.Workbook()

    # ── Sheet 1+: All non-200 rows (broken / errors / redirects) ──
    def make_data_sheet(wb, title, rows, is_first=False):
        ws = wb.active if is_first else wb.create_sheet(title)
        ws.title = title
        for ci, (h, w) in enumerate(zip(headers, col_widths), 1):
            cell           = ws.cell(row=1, column=ci, value=h)
            cell.font      = HDR_FONT
            cell.fill      = HDR_FILL
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border    = BORDER
            ws.column_dimensions[get_column_letter(ci)].width = w
        ws.row_dimensions[1].height = 18
        ws.freeze_panes = "A2"
        for ri, r in enumerate(rows, 2):
            if ri > EXCEL_MAX:
                break
            lm  = r.get("load_ms", -1)
            row_vals = [
                r.get("page_url",""), r.get("link_url",""),
                r.get("link_text",""), r.get("link_type",""),
                str(r.get("status","")), r.get("category",""),
                r.get("depth",""), r.get("effort",""),
                lm if lm not in (None, -1) else None,
                str(r.get("timestamp","")),
                r.get("page_ip","N/A"), r.get("link_ip","N/A"),
            ]
            fill = STATUS_FILLS.get(rc(r.get("status","")))
            for ci, val in enumerate(row_vals, 1):
                cell           = ws.cell(row=ri, column=ci, value=val)
                cell.border    = BORDER
                cell.alignment = Alignment(
                    vertical="center", wrap_text=(ci in (1, 2))
                )
                if fill:
                    cell.fill = fill
        ws.auto_filter.ref = ws.dimensions
        return ws

    # Isolate image rows so every existing sheet below is built purely
    # from link rows - byte-for-byte unaffected by whether CHECK_IMAGES
    # was on for this run.
    link_rows  = [r for r in results if r.get("resource_type", "Link") == "Link"]
    image_rows = [r for r in results if r.get("resource_type", "Link") == "Image"]

    rows_per_sht = EXCEL_MAX - 1

    # ── Sheet 1: Broken links — 4xx only, Internal/External links only.
    #    This is the actionable list a content team can fix (bad hrefs,
    #    stale nodes) - the parent page's own status (link_type="Page")
    #    isn't a "link" someone edits, and 5xx/timeouts are usually an
    #    infrastructure question rather than something to fix in content,
    #    so both are left out of this sheet and only shown in the next.
    broken_link_rows = [
        r for r in link_rows
        if str(r.get("status", "")).startswith("4")
        and r.get("link_type") in ("Internal", "External")
        and not is_bot_blocked(r)
    ]
    num_bl_sheets = max(1, -(-len(broken_link_rows) // rows_per_sht))
    for i in range(num_bl_sheets):
        chunk = broken_link_rows[i * rows_per_sht : (i + 1) * rows_per_sht]
        title = f"Broken links {i+1}" if i > 0 else "Broken links"
        make_data_sheet(wb, title, chunk, is_first=(i == 0))

    # ── Blocked by bot protection — external 401/403/429. Kept out of
    #    "Broken links" because most of these load fine in a browser; the
    #    site is just refusing the crawler. Verify by hand, don't "fix".
    blocked_rows = [r for r in link_rows if is_bot_blocked(r)]
    num_bb_sheets = max(1, -(-len(blocked_rows) // rows_per_sht))
    for i in range(num_bb_sheets):
        chunk = blocked_rows[i * rows_per_sht : (i + 1) * rows_per_sht]
        title = ("Blocked by bot protection" if i == 0
                 else f"Blocked by bot protection {i+1}")
        make_data_sheet(wb, title, chunk, is_first=False)

    # ── Sheet 2: All broken links — every non-200 row (broken, errors,
    #    redirects), unfiltered. Keeps the full picture one tab over.
    excel_rows  = [r for r in link_rows if str(r.get("status","")) != "200"]
    num_sheets   = max(1, -(-len(excel_rows) // rows_per_sht))

    for i in range(num_sheets):
        chunk = excel_rows[i * rows_per_sht : (i + 1) * rows_per_sht]
        title = f"All broken links {i+1}" if i > 0 else "All broken links"
        make_data_sheet(wb, title, chunk, is_first=False)

    # ── Sheet 3: Broken images — every image row that isn't 200/3xx. Own
    #    dedicated tab; every sheet in this workbook other than this one
    #    is built from link_rows only, so image checking never changes
    #    what they report.
    broken_image_rows = [
        r for r in image_rows
        if str(r.get("status", "")) != "200"
        and not str(r.get("status", "")).startswith("3")
    ]
    num_img_sheets = max(1, -(-len(broken_image_rows) // rows_per_sht))
    for i in range(num_img_sheets):
        chunk = broken_image_rows[i * rows_per_sht : (i + 1) * rows_per_sht]
        title = f"Broken images {i+1}" if i > 0 else "Broken images"
        make_data_sheet(wb, title, chunk, is_first=False)

    # ── FIX 6: Pivot sheet — per-parent-page breakdown ──
    # One pass builds page-level counters (for this sheet) and link-type
    # counters (for the Summary sheet's Working/Broken matrix) together,
    # so the two sheets can never disagree about what counts as "broken":
    # 4xx (any, not just 404/410) + 5xx + Timeout/ConnError/*Error.
    c4xx_ctr     = Counter()
    c5xx_ctr     = Counter()
    timeout_ctr  = Counter()
    redirect_ctr = Counter()
    ok_ctr       = Counter()
    type_working = Counter()
    type_broken  = Counter()

    for r in link_rows:
        s  = str(r.get("status", ""))
        pu = r.get("page_url", "")
        lt = r.get("link_type", "Other")
        if s == "200":
            ok_ctr[pu] += 1
            type_working[lt] += 1
        elif s.startswith("3"):
            redirect_ctr[pu] += 1
            type_working[lt] += 1
        elif s.startswith("5"):
            c5xx_ctr[pu] += 1
            type_broken[lt] += 1
        elif "Error" in s or "Timeout" in s:
            timeout_ctr[pu] += 1
            type_broken[lt] += 1
        else:
            # 4xx, plus any stray/uncategorized status - still "broken" so
            # page totals always reconcile with Total Links.
            c4xx_ctr[pu] += 1
            type_broken[lt] += 1

    def _broken_n(pu):
        return c4xx_ctr.get(pu, 0) + c5xx_ctr.get(pu, 0) + timeout_ctr.get(pu, 0)

    def _page_vals(pu):
        return (c4xx_ctr.get(pu, 0), c5xx_ctr.get(pu, 0), timeout_ctr.get(pu, 0),
                redirect_ctr.get(pu, 0), ok_ctr.get(pu, 0))

    all_pages     = set(c4xx_ctr) | set(c5xx_ctr) | set(timeout_ctr) | \
                    set(redirect_ctr) | set(ok_ctr)
    broken_pages  = sorted((p for p in all_pages if _broken_n(p) > 0),
                           key=lambda p: -_broken_n(p))
    working_pages = sorted((p for p in all_pages if _broken_n(p) == 0),
                           key=lambda p: -(ok_ctr.get(p, 0) + redirect_ctr.get(p, 0)))

    ws_pivot = wb.create_sheet("Broken by Page")
    pvt_headers = ["Parent URL", "4xx Errors", "5xx Errors",
                   "Timeouts / Conn Errors", "Redirects (3xx)",
                   "200 OK", "Total Links"]
    pvt_widths  = [75, 12, 12, 20, 15, 12, 14]

    for ci, (h, w) in enumerate(zip(pvt_headers, pvt_widths), 1):
        cell           = ws_pivot.cell(row=1, column=ci, value=h)
        cell.font      = HDR_FONT
        cell.fill      = HDR_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border    = BORDER
        ws_pivot.column_dimensions[get_column_letter(ci)].width = w
    ws_pivot.row_dimensions[1].height = 18
    ws_pivot.freeze_panes = "A2"

    HIGH_FILL  = PatternFill("solid", fgColor="F8D7DA")  # red   — has broken links
    WARN_FILL  = PatternFill("solid", fgColor="FFF3CD")  # amber — redirects only
    CLEAN_FILL = PatternFill("solid", fgColor="D4EDDA")  # green — all OK
    TOTAL_FILL = PatternFill("solid", fgColor="1F4E79")  # dark blue — totals row

    def _write_page_row(ri, pu, fill):
        vals = _page_vals(pu)
        for ci, val in enumerate([pu, *vals, sum(vals)], 1):
            cell           = ws_pivot.cell(row=ri, column=ci, value=val)
            cell.border    = BORDER
            cell.fill      = fill
            cell.alignment = Alignment(vertical="center", wrap_text=(ci == 1))
            if ci == 1 and val:
                cell.font = Font(color="1155CC", underline="single")

    def _write_total_row(ri, label, pages):
        sums = [0, 0, 0, 0, 0]
        for pu in pages:
            for i, v in enumerate(_page_vals(pu)):
                sums[i] += v
        for ci, val in enumerate([label, *sums, sum(sums)], 1):
            cell           = ws_pivot.cell(row=ri, column=ci, value=val)
            cell.border    = BORDER
            cell.fill      = TOTAL_FILL
            cell.font      = Font(bold=True, color="FFFFFF")
            cell.alignment = Alignment(vertical="center")

    # Broken pages first, a Total Broken row, then a genuinely blank row
    # (all cells empty) before the working pages - so Ctrl+Down from
    # anywhere in the broken block lands on Total Broken instead of
    # scrolling through however many thousand clean pages follow it.
    ri = 2
    for pu in broken_pages:
        _write_page_row(ri, pu, HIGH_FILL)
        ri += 1
    _write_total_row(ri, f"Total Broken ({len(broken_pages):,} pages)", broken_pages)
    ri += 2  # totals row, then one fully-blank separator row

    for pu in working_pages:
        fill = WARN_FILL if redirect_ctr.get(pu, 0) > 0 else CLEAN_FILL
        _write_page_row(ri, pu, fill)
        ri += 1
    _write_total_row(ri, f"Total Working ({len(working_pages):,} pages)", working_pages)

    ws_pivot.auto_filter.ref = ws_pivot.dimensions

    # ── Summary sheet ──
    ws2 = wb.create_sheet("Summary")
    ws2.column_dimensions["A"].width = 28
    ws2.column_dimensions["B"].width = 20
    ws2.column_dimensions["C"].width = 14
    ws2.column_dimensions["D"].width = 14

    c4xx_total    = sum(c4xx_ctr.values())
    c5xx_total    = sum(c5xx_ctr.values())
    timeout_total = sum(timeout_ctr.values())
    rds_total     = sum(redirect_ctr.values())
    ok_total      = sum(ok_ctr.values())
    pages         = len({r["page_url"] for r in link_rows})

    header_rows = [
        ("Broken Link Report", None),
        ("Site", CONFIG["BASE_URL"]),
        ("Start Date and Time", start_dt or "N/A"),
        ("End Date and Time", end_dt or now_est().strftime("%Y-%m-%d %H:%M:%S")),
        ("Total Run time", duration_str or "N/A"),
    ]
    ri2 = 1
    for label, value in header_rows:
        c = ws2.cell(row=ri2, column=1, value=label)
        c.font = Font(bold=True, size=14, color="1F4E79") if ri2 == 1 else BOLD
        if value is not None:
            ws2.cell(row=ri2, column=2, value=value)
        ri2 += 1
    ri2 += 1  # blank row

    # Working vs Broken by link type — the direct answer to "where did the
    # N broken links go": split across Internal / External / the crawler's
    # own always-recorded page-self status row.
    for ci, h in enumerate(["Link Type", "Working", "Broken", "Total"], 1):
        cell = ws2.cell(row=ri2, column=ci, value=h)
        cell.font      = HDR_FONT
        cell.fill      = HDR_FILL
        cell.border    = BORDER
        cell.alignment = Alignment(horizontal="center")
    ri2 += 1

    grand_working = grand_broken = 0
    for key, label in [("Page", "Page (self)"), ("Internal", "Internal"), ("External", "External")]:
        w, b = type_working.get(key, 0), type_broken.get(key, 0)
        grand_working += w
        grand_broken  += b
        for ci, val in enumerate([label, w, b, w + b], 1):
            cell = ws2.cell(row=ri2, column=ci, value=val)
            cell.border    = BORDER
            cell.alignment = Alignment(horizontal="left" if ci == 1 else "center")
        ri2 += 1
    for ci, val in enumerate(["All Links", grand_working, grand_broken, grand_working + grand_broken], 1):
        cell = ws2.cell(row=ri2, column=ci, value=val)
        cell.font      = BOLD
        cell.border    = BORDER
        cell.fill      = PatternFill("solid", fgColor="E9ECEF")
        cell.alignment = Alignment(horizontal="left" if ci == 1 else "center")
    ri2 += 2  # totals row, then a blank row

    detail_rows = [
        ("Metric", "Count"),
        ("Parent Pages", pages),
        ("Total Links", len(link_rows)),
        ("200 OK", ok_total),
        ("Redirects (3xx)", rds_total),
        ("4xx Errors", c4xx_total),
        ("Server Errors (5xx)", c5xx_total),
        ("Timeouts / Conn Errors", timeout_total),
        ("External blocked by bot protection (401/403/429, included in 4xx)",
         len(blocked_rows)),
    ]
    for label, value in detail_rows:
        c = ws2.cell(row=ri2, column=1, value=label)
        if label == "Metric":
            c.font, c.fill = HDR_FONT, HDR_FILL
            vc = ws2.cell(row=ri2, column=2, value=value)
            vc.font, vc.fill = HDR_FONT, HDR_FILL
        else:
            c.font = BOLD
            ws2.cell(row=ri2, column=2, value=value)
        ri2 += 1

    wb.save(path)
    log.info("Excel written → %s  (%d issues + pivot)", path, len(excel_rows))


# ─────────────────────────────────────────────────────────────
#  SKIPPED URLS HTML
# ─────────────────────────────────────────────────────────────

def build_skipped_html(
    skipped_snapshot: dict, base_url: str, run_date: str, _skipped_file: str
) -> str:
    if not skipped_snapshot:
        return ""

    REASON_LABELS = {
        "trap":           "Trap pattern match",
        "doc_ext":        "Document extension (not crawled for links)",
        "binary_ext":     "Binary/media file (not crawled for links)",
        "depth_exceeded": "Exceeded MAX_DEPTH",
    }
    total_skipped = sum(len(v) for v in skipped_snapshot.values())
    sections_html = ""

    for reason, entries in sorted(skipped_snapshot.items(), key=lambda x: -len(x[1])):
        label      = REASON_LABELS.get(reason, reason)
        pat_counts = Counter(pat for _, pat in entries)
        rows       = ""
        for url, pat in entries:
            pc = (
                f'<code style="background:#1e293b;padding:1px 6px;border-radius:3px;'
                f'font-size:11px">{pat}</code>' if pat else "—"
            )
            rows += (
                f'<tr><td><a href="{url}" target="_blank">{url}</a></td>'
                f'<td style="white-space:nowrap">{pc}</td></tr>'
            )
        pat_summary = ", ".join(
            f"{p} ({n})" for p, n in pat_counts.most_common(5) if p
        )
        sections_html += f"""
<div style="margin-bottom:32px">
  <h2 style="color:#e2e8f0;font-size:16px;margin-bottom:6px">{label}
    <span style="color:#64748b;font-size:13px;font-weight:400">— {len(entries):,} URLs</span>
  </h2>
  {"<p style='color:#94a3b8;font-size:12px;margin-bottom:8px'>Top patterns: " + pat_summary + "</p>" if pat_summary else ""}
  <div style="overflow-x:auto;max-height:500px;overflow-y:auto">
  <table><thead><tr><th>URL</th><th>Pattern matched</th></tr></thead>
  <tbody>{rows}</tbody></table></div></div>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>Skipped URLs — {base_url}</title>
<style>
  body{{background:#0f172a;color:#e2e8f0;font-family:-apple-system,sans-serif;
       font-size:13px;margin:0;padding:24px 32px}}
  a{{color:#60a5fa;text-decoration:none}} a:hover{{text-decoration:underline}}
  table{{width:100%;border-collapse:collapse;font-size:12px}}
  thead th{{background:#1e293b;padding:8px 12px;text-align:left;color:#94a3b8;
            border-bottom:1px solid #334155}}
  tbody tr:hover{{background:#1e293b}}
  td{{padding:6px 12px;vertical-align:top;word-break:break-all}}
  .back{{display:inline-block;margin-bottom:20px;color:#60a5fa}}
</style></head><body>
<a href="index.html" class="back">← Back to main report</a>
<h1 style="font-size:1.2rem;margin-bottom:4px">Skipped URLs — {base_url}</h1>
<p style="color:#64748b;font-size:12px;margin-bottom:24px">
  Run: {run_date} &nbsp;|&nbsp;
  {total_skipped:,} total skipped &nbsp;|&nbsp;
  {len(skipped_snapshot)} categories
</p>
{sections_html}
</body></html>"""


# ─────────────────────────────────────────────────────────────
#  HTML REPORT  — FIX: data written to separate JSON file
#  The HTML shell is ~200 KB; the JSON data file can be any size.
#  Open via:  python serve.py   (in the reports folder)
# ─────────────────────────────────────────────────────────────

def build_html_report(
    results:            list[dict],
    csv_path:           str,
    elapsed:            float,
    scan_mode:          str  = "full",
    target_pages:       list = None,
    dropped_urls:       list = None,
    skipped_snapshot:   dict = None,
    data_json_filename: str  = "report_data.json",
    truncated:          bool = False,
    truncated_count:    int  = 0,
    inline_data:        list = None,
) -> str:
    PAGE_SIZE = 100

    # Embedding the row data directly in the page means it opens with a
    # plain double-click (file://) - no server, nothing to share alongside
    # it. `fetch()` is blocked outright for file:// pages in Chrome/Firefox,
    # which is what "Cannot load data file" actually means; it has nothing
    # to do with file permissions. Caller decides whether inline_data is
    # worth passing (a huge crawl's JSON could be hundreds of MB, too big
    # to usefully inline into one HTML document) - when it isn't, this
    # falls back to the original fetch(DATA_FILE) path unchanged, so a
    # served/huge report still works exactly as before.
    inline_data_script = ""
    if inline_data is not None:
        # Raw text search, not JS-aware - the HTML tokenizer looks for the
        # literal bytes "</script" while scanning, regardless of this
        # script's type="application/json" (which only affects whether the
        # browser executes it as code). A page whose crawled content
        # happens to contain that substring (an anchor's link text, say)
        # would otherwise truncate the JSON and break the report.
        inline_json = json.dumps(inline_data, ensure_ascii=False).replace("</", "<\\/")
        inline_data_script = (
            f'<script type="application/json" id="report-data-json">'
            f'{inline_json}</script>'
        )

    total_links   = len(results)
    pages_set     = {r["page_url"] for r in results}
    total_pages   = len(pages_set)
    broken        = sum(1 for r in results if str(r["status"]) in ("404","410","451"))
    redirects     = sum(1 for r in results if str(r["status"]).startswith("3"))
    server_errors = sum(1 for r in results if str(r["status"]).startswith("5"))
    ok_links      = sum(1 for r in results if str(r["status"]) == "200")
    errors        = sum(
        1 for r in results
        if "Error" in str(r["status"]) or "Timeout" in str(r["status"])
    )
    external = sum(1 for r in results if r.get("link_type") == "External")
    internal = sum(1 for r in results if r.get("link_type") == "Internal")

    cat_counts: dict[str, int] = {}
    for r in results:
        cat = r.get("category", "Unknown")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    broken_by_page: dict[str, int] = {}
    for r in results:
        s = str(r["status"])
        if s in ("404","410","451") or "Error" in s or "Timeout" in s:
            broken_by_page[r["page_url"]] = broken_by_page.get(r["page_url"], 0) + 1
    top_broken = sorted(broken_by_page.items(), key=lambda x: -x[1])[:10]

    chart_labels = json.dumps(list(cat_counts.keys()))
    chart_values = json.dumps(list(cat_counts.values()))
    chart_colors = json.dumps([
        "#22c55e","#16a34a","#f59e0b","#ef4444","#dc2626",
        "#3b82f6","#8b5cf6","#64748b","#0ea5e9","#f97316",
    ][:len(cat_counts)])
    bar_labels = json.dumps(
        [p[:60] + "…" if len(p) > 60 else p for p, _ in top_broken]
    )
    bar_values = json.dumps([v for _, v in top_broken])

    visible_rows      = total_links - truncated_count  # rows actually embedded in this HTML
    csv_path_basename = Path(csv_path).name            # filename only for download link
    run_date   = now_est().strftime("%Y-%m-%d %H:%M:%S")
    run_dur    = fmt_duration(elapsed)
    base_url   = CONFIG["BASE_URL"]
    scan_label = CONFIG.get("SCAN_LABEL", "")
    mode_badge = (
        '<span style="background:#7c3aed;color:#fff;padding:2px 10px;'
        'border-radius:12px;font-size:12px;font-weight:700;margin-left:10px">'
        'TARGETED SCAN</span>'
        if scan_mode == "targeted" else
        '<span style="background:#0369a1;color:#fff;padding:2px 10px;'
        'border-radius:12px;font-size:12px;font-weight:700;margin-left:10px">'
        'FULL CRAWL</span>'
    )
    target_count = len(target_pages) if target_pages else 0

    # Targeted info banner
    if scan_mode == "targeted" and target_pages:
        tp_items   = "".join(
            f'<li><a href="{u}" target="_blank" style="color:#93c5fd">{u}</a></li>'
            for u in target_pages
        )
        label_html = (
            '<b style="color:#94a3b8">Label: </b>'
            '<span style="color:#e2e8f0">' + scan_label + '</span><br><br>'
        ) if scan_label else ""
        targeted_info_html = (
            '<div style="background:#1e293b;border:1px solid #7c3aed;'
            'border-radius:10px;padding:14px 18px;margin-bottom:18px">'
            '<div style="font-weight:700;color:#c4b5fd;margin-bottom:8px">'
            f"🎯 Targeted Scan — {target_count} page(s) scanned"
            '</div>' + label_html +
            '<ul style="list-style:none;padding:0;display:flex;'
            'flex-direction:column;gap:4px">'
            + tp_items + '</ul></div>'
        )
    else:
        targeted_info_html = ""

    # Truncation warning banner
    # NOTE: total_links = len(results) = true total after dedup.
    # truncated_count = total_links - len(display_results)
    # visible_rows    = total_links - truncated_count = len(display_results)
    non_ok_count = total_links - ok_links  # all non-200 rows, always fully included
    if truncated:
        truncated_html = (
            '<div style="background:#1e293b;border:1px solid #f59e0b;'
            'border-radius:10px;padding:14px 18px;margin-bottom:18px;font-size:13px">'
            f'<b style="color:#fbbf24">⚠️ Large scan — table shows {total_links - truncated_count:,} of '
            f'{total_links:,} total rows</b>'
            f'&nbsp;(HTML_MAX_ROWS={CONFIG["HTML_MAX_ROWS"]:,})<br>'
            f'<span style="color:#94a3b8;font-size:12px">'
            f'All <b style="color:#f87171">{non_ok_count:,} non-200 rows</b> (broken, errors, redirects) '
            f'are fully included. Only {truncated_count:,} OK rows were trimmed to keep the browser responsive.<br>'
            f'For all data use: the <b>script-generated Excel</b> ("All broken links" sheet = all {non_ok_count:,} non-200 rows) '
            f'or the <b>Full CSV</b> file in the reports folder.</span>'
            '</div>'
        )
    else:
        truncated_html = ""

    # Dropped URLs banner
    dropped_urls   = dropped_urls or []
    if dropped_urls:
        seen_d     = set()
        unique_d   = [u for u in dropped_urls if not (u in seen_d or seen_d.add(u))]
        dropped_csv = ",".join(unique_d[:500])
        more_note   = (
            f" (showing first 500 of {len(unique_d):,})" if len(unique_d) > 500 else ""
        )
        dropped_rows_html = "".join(
            f'<tr><td><a href="{u}" target="_blank" style="color:#93c5fd">{u}</a></td></tr>'
            for u in unique_d[:500]
        )
        dropped_info_html = (
            '<div style="background:#1e293b;border:1px solid #f59e0b;'
            'border-radius:10px;padding:14px 18px;margin-bottom:18px">'
            f'<div style="font-weight:700;color:#fbbf24;margin-bottom:8px">'
            f'{len(unique_d):,} pages NOT crawled — MAX_QUEUE cap reached{more_note}</div>'
            '<p style="color:#94a3b8;font-size:13px;margin-bottom:10px">'
            'Paste the comma-separated list below into <b>TARGET_PAGES</b> '
            'for a follow-up targeted scan, or use the dropped_pages.txt file.</p>'
            '<div style="background:#0f172a;border-radius:6px;padding:10px;margin-bottom:10px">'
            '<div style="color:#64748b;font-size:11px;margin-bottom:6px">'
            '▼ Comma-separated — paste into TARGET_PAGES env var</div>'
            f'<textarea readonly onclick="this.select()" style="width:100%;height:80px;'
            'background:#0f172a;color:#a3e635;border:1px solid #334155;'
            'border-radius:4px;padding:8px;font-size:11px;'
            f'font-family:monospace;resize:vertical">{dropped_csv}</textarea></div>'
            f'<details><summary style="color:#94a3b8;font-size:13px;cursor:pointer">'
            f'Show all {len(unique_d):,} dropped URLs</summary>'
            '<div style="max-height:300px;overflow-y:auto;margin-top:8px">'
            '<table style="width:100%;border-collapse:collapse"><thead><tr>'
            '<th style="background:#1e293b;padding:6px 10px;text-align:left;'
            'color:#94a3b8;font-size:11px;border-bottom:1px solid #334155">URL</th>'
            f'</tr></thead><tbody style="font-size:12px">'
            f'{dropped_rows_html}</tbody></table></div></details></div>'
        )
    else:
        dropped_info_html = ""

    # Skipped summary
    skipped_snapshot = skipped_snapshot or {}
    if skipped_snapshot:
        REASON_LABELS = {
            "trap":           "Trap pattern match",
            "doc_ext":        "Document extension",
            "binary_ext":     "Binary/media file",
            "depth_exceeded": "Exceeded MAX_DEPTH",
        }
        total_sk      = sum(len(v) for v in skipped_snapshot.values())
        summary_rows  = ""
        for reason, entries in sorted(
            skipped_snapshot.items(), key=lambda x: -len(x[1])
        ):
            label      = REASON_LABELS.get(reason, reason)
            pat_counts = Counter(pat for _, pat in entries)
            top_pats   = ", ".join(
                f"<code style='background:#0f172a;padding:1px 4px;border-radius:3px;"
                f"font-size:11px'>{p}</code>"
                for p, _ in pat_counts.most_common(4) if p
            ) or "—"
            summary_rows += (
                f'<tr><td style="padding:6px 12px;color:#e2e8f0">{label}</td>'
                f'<td style="padding:6px 12px;text-align:right;font-weight:700">'
                f'{len(entries):,}</td>'
                f'<td style="padding:6px 12px">{top_pats}</td></tr>'
            )
        skipped_info_html = (
            '<div style="background:#1e293b;border:1px solid #334155;'
            'border-radius:10px;padding:14px 18px;margin-bottom:18px">'
            '<div style="display:flex;align-items:center;'
            'justify-content:space-between;margin-bottom:10px">'
            f'<span style="font-weight:700;color:#e2e8f0;font-size:14px">'
            f'Skipped URL Summary — {total_sk:,} URLs not crawled for links</span>'
            f'<a href="skipped_urls.html" target="_blank" style="background:#334155;'
            'color:#93c5fd;padding:4px 14px;border-radius:6px;font-size:12px;'
            'font-weight:600;text-decoration:none">View full list</a>'
            '</div>'
            '<table style="width:100%;border-collapse:collapse;font-size:12px">'
            '<thead><tr style="border-bottom:1px solid #334155">'
            '<th style="padding:6px 12px;text-align:left;color:#64748b">Reason</th>'
            '<th style="padding:6px 12px;text-align:right;color:#64748b">Count</th>'
            '<th style="padding:6px 12px;text-align:left;color:#64748b">Top patterns</th>'
            f'</tr></thead><tbody>{summary_rows}</tbody></table></div>'
        )
    else:
        skipped_info_html = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Broken Link Report — {base_url}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.sheetjs.com/xlsx-0.20.1/package/dist/xlsx.full.min.js"></script>
<style>
:root{{
  --bg:#0f172a;--surface:#1e293b;--surface2:#263348;
  --border:#334155;--text:#e2e8f0;--muted:#94a3b8;
  --green:#22c55e;--yellow:#f59e0b;--red:#ef4444;
  --blue:#3b82f6;--purple:#8b5cf6;--orange:#f97316;
  --radius:10px;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px}}
a{{color:#60a5fa;text-decoration:none}} a:hover{{text-decoration:underline}}
.header{{background:var(--surface);border-bottom:1px solid var(--border);
         padding:16px 28px;display:flex;align-items:center;
         justify-content:space-between;flex-wrap:wrap;gap:12px}}
.header h1{{font-size:1.25rem;font-weight:700}}
.header h1 span{{color:var(--blue)}}
.meta{{color:var(--muted);font-size:12px;display:flex;gap:16px;
       flex-wrap:wrap;margin-top:4px}}
.meta b{{color:var(--text)}}
.hdr-btns{{display:flex;gap:8px;flex-wrap:wrap}}
.container{{max-width:1600px;margin:0 auto;padding:20px 28px}}
.loading-overlay{{position:fixed;inset:0;background:rgba(15,23,42,.92);
  z-index:9999;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:16px}}
.spinner{{width:48px;height:48px;border:4px solid #334155;
  border-top-color:var(--blue);border-radius:50%;
  animation:spin .8s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.load-msg{{color:var(--muted);font-size:14px}}
.load-error{{display:none;background:#1e293b;border:1px solid #ef4444;
  border-radius:10px;padding:24px 32px;max-width:560px;text-align:center}}
.load-error h2{{color:#f87171;margin-bottom:12px}}
.load-error code{{background:#0f172a;padding:6px 12px;border-radius:6px;
  display:inline-block;margin:8px 0;font-size:13px;color:#a3e635}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));
        gap:12px;margin-bottom:22px}}
.card{{background:var(--surface);border:1px solid var(--border);
       border-radius:var(--radius);padding:14px 16px;
       cursor:pointer;transition:border-color .15s}}
.card:hover{{border-color:var(--blue)}}
.card.active{{border-color:var(--blue);background:var(--surface2)}}
.card .val{{font-size:1.8rem;font-weight:800;line-height:1;margin-bottom:3px}}
.card .lbl{{color:var(--muted);font-size:11px;text-transform:uppercase;
            letter-spacing:.06em}}
.c-green .val{{color:var(--green)}} .c-red .val{{color:var(--red)}}
.c-yellow .val{{color:var(--yellow)}} .c-blue .val{{color:var(--blue)}}
.c-orange .val{{color:var(--orange)}} .c-purple .val{{color:var(--purple)}}
.c-white .val{{color:var(--text)}}
.charts{{display:grid;grid-template-columns:300px 1fr;gap:16px;margin-bottom:22px}}
@media(max-width:860px){{.charts{{grid-template-columns:1fr}}}}
.chart-box{{background:var(--surface);border:1px solid var(--border);
            border-radius:var(--radius);padding:16px}}
.chart-box h2{{font-size:.8rem;font-weight:600;margin-bottom:12px;
               color:var(--muted);text-transform:uppercase;letter-spacing:.06em}}
.chart-box canvas{{max-height:250px}}
.filters{{background:var(--surface);border:1px solid var(--border);
          border-radius:var(--radius);padding:12px 16px;margin-bottom:12px;
          display:flex;flex-wrap:wrap;gap:10px;align-items:center}}
.filters label{{color:var(--muted);font-size:12px;margin-right:3px}}
.filters input,.filters select{{background:var(--surface2);
  border:1px solid var(--border);color:var(--text);
  padding:6px 10px;border-radius:6px;font-size:13px;outline:none}}
.filters input{{width:220px}}
.filters input:focus,.filters select:focus{{border-color:var(--blue)}}
.info{{margin-left:auto;color:var(--muted);font-size:12px;white-space:nowrap}}
.export-row{{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;align-items:center}}
.export-row span{{color:var(--muted);font-size:12px}}
.btn{{background:var(--blue);color:#fff;border:none;padding:7px 14px;
      border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;white-space:nowrap}}
.btn:hover{{background:#2563eb}}
.btn-green{{background:#16a34a}} .btn-green:hover{{background:#15803d}}
.btn-ghost{{background:transparent;border:1px solid var(--border);color:var(--muted)}}
.btn-ghost:hover{{border-color:var(--blue);color:var(--text)}}
.table-wrap{{background:var(--surface);border:1px solid var(--border);
             border-radius:var(--radius);overflow:auto}}
table{{width:100%;border-collapse:collapse}}
thead th{{background:var(--surface2);padding:9px 12px;text-align:left;
          font-size:11px;text-transform:uppercase;letter-spacing:.06em;
          color:var(--muted);border-bottom:1px solid var(--border);
          white-space:nowrap;cursor:pointer;user-select:none}}
thead th:hover{{color:var(--text)}}
tbody tr{{border-bottom:1px solid var(--border)}}
tbody tr:hover{{background:var(--surface2)}}
td{{padding:8px 12px;vertical-align:middle;max-width:280px;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.td-load{{text-align:right;font-variant-numeric:tabular-nums;
          color:var(--muted);font-size:12px}}
.load-slow{{color:#f87171}} .load-med{{color:#fbbf24}} .load-ok{{color:#4ade80}}
.row-redirect{{background:rgba(245,158,11,.04)}}
.row-broken{{background:rgba(239,68,68,.07)}}
.row-warn{{background:rgba(249,115,22,.05)}}
.row-servererr{{background:rgba(139,92,246,.05)}}
.row-unkn{{background:rgba(148,163,184,.04)}}
.pill{{display:inline-block;padding:2px 8px;border-radius:12px;
       font-size:12px;font-weight:700}}
.p-ok{{background:rgba(34,197,94,.15);color:#4ade80}}
.p-redirect{{background:rgba(245,158,11,.15);color:#fbbf24}}
.p-broken{{background:rgba(239,68,68,.2);color:#f87171}}
.p-warn{{background:rgba(249,115,22,.15);color:#fb923c}}
.p-servererr{{background:rgba(139,92,246,.15);color:#a78bfa}}
.p-unkn{{background:rgba(148,163,184,.15);color:#94a3b8}}
.tt{{display:inline-block;padding:2px 6px;border-radius:4px;
     font-size:11px;font-weight:600}}
.tt-internal{{background:rgba(59,130,246,.15);color:#93c5fd}}
.tt-external{{background:rgba(139,92,246,.15);color:#c4b5fd}}
.tt-page{{background:rgba(148,163,184,.15);color:#94a3b8}}
.bdg{{display:inline-block;padding:2px 6px;border-radius:4px;
      font-size:11px;font-weight:600}}
.b-none{{background:rgba(34,197,94,.1);color:#4ade80}}
.b-low{{background:rgba(245,158,11,.1);color:#fbbf24}}
.b-lowmed{{background:rgba(249,115,22,.1);color:#fb923c}}
.b-med{{background:rgba(139,92,246,.1);color:#a78bfa}}
.b-high{{background:rgba(239,68,68,.15);color:#f87171}}
.pagination{{display:flex;align-items:center;gap:6px;
             padding:10px 14px;border-top:1px solid var(--border);flex-wrap:wrap}}
.pagination button{{background:var(--surface2);border:1px solid var(--border);
  color:var(--text);padding:4px 10px;border-radius:5px;
  cursor:pointer;font-size:12px}}
.pagination button:hover{{border-color:var(--blue)}}
.pagination button:disabled{{opacity:.35;cursor:not-allowed}}
.pagination button.active{{background:var(--blue);border-color:var(--blue);color:#fff}}
.pginfo{{color:var(--muted);font-size:12px;margin:0 4px}}
.sort-asc::after{{content:" ▲";opacity:.8;font-size:9px}}
.sort-desc::after{{content:" ▼";opacity:.8;font-size:9px}}
</style>
</head>
<body>

<!-- Loading overlay (shown while JSON data file is being fetched) -->
<div class="loading-overlay" id="loading-overlay">
  <div class="spinner"></div>
  <div class="load-msg" id="load-msg">Loading report data…</div>
  <div class="load-error" id="load-error">
    <h2>Cannot load data file</h2>
    <p style="color:#94a3b8;margin-bottom:12px">
      This report is too large to embed directly, so the browser needs to
      fetch its data file separately - which only works when the report is
      served over HTTP, not opened straight from disk.
    </p>
    <p style="color:#94a3b8;margin-bottom:12px">
      Easiest: open it from <code>serve.py</code>'s report browser, or the
      Test Tools webapp's Crawl Report tab, instead of double-clicking the
      .html file.
    </p>
    <p style="color:#94a3b8;margin-bottom:12px">Or from a terminal in the reports folder:</p>
    <code>python -m http.server 8088</code>
    <p style="color:#94a3b8;margin-top:12px">
      Then open
      <a href="http://localhost:8088" style="color:#60a5fa">
        http://localhost:8088
      </a>
    </p>
    <p style="color:#64748b;font-size:12px;margin-top:8px" id="error-detail"></p>
  </div>
</div>

<div class="header">
  <div>
    <h1>Broken Link Report — <span>{base_url}</span>{mode_badge}</h1>
    <div class="meta">
      <span>Run date: <b>{run_date}</b></span>
      <span>Duration: <b>{run_dur}</b></span>
      {"<span>Pages scanned: <b>" + str(target_count) + "</b></span>"
        if scan_mode == "targeted"
        else f'<span>Depth: <b>{CONFIG["MAX_DEPTH"]}</b></span>'}
      <span>Workers: <b>{CONFIG["CONCURRENCY"]}</b></span>
      {"<span>Label: <b>" + scan_label + "</b></span>" if scan_label else ""}
      {"<span style='color:#f59e0b'>Table: <b>" + f"{visible_rows:,}" + " of " + f"{total_links:,}" + " rows</b></span>" if truncated else ""}
    </div>
  </div>
  <div class="hdr-btns">
    <a href="/"
       style="background:#334155;color:#93c5fd;padding:7px 14px;border-radius:6px;
              font-size:12px;font-weight:600;text-decoration:none;white-space:nowrap"
       title="Go to report list (requires serve.py running)">
      &#8592; All Reports
    </a>
    <a href="{csv_path_basename}" download
       style="background:#16a34a;color:#fff;padding:7px 14px;border-radius:6px;
              font-size:13px;font-weight:600;text-decoration:none;white-space:nowrap"
       title="Download the complete {total_links:,}-row CSV generated by the crawler">
      ⬇ Full CSV (all {total_links:,} rows)
    </a>
    <button class="btn" onclick="exportCsvFiltered()"
            title="Export the current filtered table view">
      ⬇ Filtered CSV
    </button>
    <button class="btn btn-green" onclick="exportExcelFull()"
            title="Export rows currently loaded in this report ({visible_rows:,} rows). For all data use the Full CSV link.">
      ⬇ Table Excel ({visible_rows:,} rows)
    </button>
  </div>
</div>

<div class="container">
{targeted_info_html}
{truncated_html}
{dropped_info_html}
{skipped_info_html}

  <div class="cards">
    <div class="card c-white"  onclick="cardFilter('')"          id="card-all">
      <div class="val">{total_pages:,}</div><div class="lbl">Parent Pages</div></div>
    <div class="card c-white"  onclick="cardFilter('')"          id="card-links">
      <div class="val">{total_links:,}</div><div class="lbl">Total Links</div></div>
    <div class="card c-green"  onclick="cardFilter('ok')"        id="card-ok">
      <div class="val">{ok_links:,}</div><div class="lbl">200 OK</div></div>
    <div class="card c-yellow" onclick="cardFilter('redirect')"  id="card-redirect">
      <div class="val">{redirects:,}</div><div class="lbl">Redirects (3xx)</div></div>
    <div class="card c-red"    onclick="cardFilter('broken')"    id="card-broken">
      <div class="val">{broken:,}</div><div class="lbl">Broken (404/410)</div></div>
    <div class="card c-purple" onclick="cardFilter('servererr')" id="card-5xx">
      <div class="val">{server_errors:,}</div><div class="lbl">Server Errors (5xx)</div></div>
    <div class="card c-orange" onclick="cardFilter('unkn')"      id="card-err">
      <div class="val">{errors:,}</div><div class="lbl">Timeout / Error</div></div>
    <div class="card c-blue"   onclick="typeFilter2('internal')" id="card-int">
      <div class="val">{internal:,}</div><div class="lbl">Internal Links</div></div>
    <div class="card c-white"  onclick="typeFilter2('external')" id="card-ext">
      <div class="val">{external:,}</div><div class="lbl">External Links</div></div>
  </div>

  <div style="display:flex;justify-content:flex-end;margin-bottom:12px">
    <label style="display:flex;align-items:center;gap:6px;
                  cursor:pointer;color:#94a3b8;font-size:13px">
      <input type="checkbox" id="hide200" onchange="applyFilters()"
             style="width:14px;height:14px">
      Hide 200 OK rows
    </label>
  </div>

  <div class="charts">
    <div class="chart-box"><h2>Status Distribution</h2>
      <canvas id="donut"></canvas></div>
    <div class="chart-box"><h2>Top Parent Pages by Broken / Error Links</h2>
      <canvas id="bar"></canvas></div>
  </div>

  <div class="filters">
    <div><label>Search</label>
      <input type="text" id="search"
             placeholder="Filter by URL or anchor text…"
             oninput="applyFilters()"></div>
    <div><label>Status</label>
      <select id="statusFilter" onchange="applyFilters()">
        <option value="">All Statuses</option>
        <option value="ok">200 OK</option>
        <option value="redirect">Redirect (3xx)</option>
        <option value="broken">Broken (404/410)</option>
        <option value="warn">4xx Other</option>
        <option value="servererr">5xx Server Error</option>
        <option value="unkn">Timeout / Error</option>
      </select></div>
    <div><label>Type</label>
      <select id="typeFilter" onchange="applyFilters()">
        <option value="">All Types</option>
        <option value="Internal">Internal</option>
        <option value="External">External</option>
      </select></div>
    <div><label>Domain</label>
      <input type="text" id="domainFilter" placeholder="e.g. x.com"
             oninput="applyFilters()" style="width:160px"></div>
    <div><label>Effort</label>
      <select id="effortFilter" onchange="applyFilters()">
        <option value="">All Efforts</option>
        <option value="High">High</option>
        <option value="Medium">Medium</option>
        <option value="Low">Low</option>
        <option value="None">None</option>
      </select></div>
    <button class="btn btn-ghost" onclick="resetFilters()">Reset</button>
    <span class="info" id="rowInfo"></span>
  </div>

  <div class="export-row">
    <span>Export current filter view:</span>
    <button class="btn btn-green" onclick="exportExcelFiltered()">
      ⬇ Filtered Excel</button>
    <button class="btn" onclick="exportCsvFiltered()">⬇ Filtered CSV</button>
    <span class="info" id="filteredCount"></span>
  </div>

  <div class="table-wrap">
    <table>
      <thead id="thead"><tr>
        <th onclick="sortBy(0)">Parent URL</th>
        <th onclick="sortBy(1)">Link URL</th>
        <th onclick="sortBy(2)">Anchor Text</th>
        <th onclick="sortBy(3)">Type</th>
        <th onclick="sortBy(4)">Status</th>
        <th onclick="sortBy(5)">Category</th>
        <th onclick="sortBy(6)">Depth</th>
        <th onclick="sortBy(7)">Effort</th>
        <th onclick="sortBy(8)">Load Time</th>
        <th onclick="sortBy(9)" title="IP of the parent page">Parent IP</th>
        <th onclick="sortBy(10)" title="IP of the link URL">Link IP</th>
        <th onclick="sortBy(11)">Timestamp</th>
        <th onclick="sortBy(12)" title="Link or Image">Resource</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
    <div class="pagination" id="pager"></div>
  </div>
</div>

{inline_data_script}
<script>
// ── Data loader ─────────────────────────────────────────────
// report-data-json (above) is present when the crawl was small enough to
// embed directly - the common case, and what makes the report openable
// via file:// with no server. Only a genuinely huge crawl falls back to
// fetching DATA_FILE as a separate request, which needs HTTP.
const DATA_FILE = '{data_json_filename}';
const PAGE_SIZE = {PAGE_SIZE};
let ALL_DATA = [];
let filtered = [];
let sortCol  = -1, sortAsc = true, curPage = 0;
let chartsInit = false;

async function loadData() {{
  const overlay  = document.getElementById('loading-overlay');
  const msgEl    = document.getElementById('load-msg');
  const errorEl  = document.getElementById('load-error');
  const detailEl = document.getElementById('error-detail');
  try {{
    const inlineEl = document.getElementById('report-data-json');
    if (inlineEl) {{
      ALL_DATA = JSON.parse(inlineEl.textContent);
    }} else {{
      msgEl.textContent = 'Fetching report data…';
      const resp = await fetch(DATA_FILE);
      if (!resp.ok) throw new Error('HTTP ' + resp.status + ' — ' + resp.statusText);
      msgEl.textContent = 'Parsing…';
      ALL_DATA = await resp.json();
    }}
    filtered = ALL_DATA.slice();
    overlay.style.display = 'none';
    initCharts();
    renderPage(0);
  }} catch(e) {{
    msgEl.style.display  = 'none';
    errorEl.style.display = 'block';
    detailEl.textContent  = 'Error: ' + e.message;
  }}
}}
loadData();

// ── Charts ───────────────────────────────────────────────────
function initCharts() {{
  if (chartsInit) return;
  chartsInit = true;
  try {{
    new Chart(document.getElementById('donut').getContext('2d'), {{
      type:'doughnut',
      data:{{
        labels:{chart_labels},
        datasets:[{{
          data:{chart_values},
          backgroundColor:{chart_colors},
          borderWidth:2,borderColor:'#1e293b'
        }}]
      }},
      options:{{responsive:true,plugins:{{
        legend:{{position:'bottom',labels:{{color:'#94a3b8',padding:10,
          font:{{size:11}}}}}},
        tooltip:{{callbacks:{{label:ctx=>` ${{ctx.label}}: ${{ctx.parsed.toLocaleString()}}`}}}}
      }}}}
    }});
    new Chart(document.getElementById('bar').getContext('2d'), {{
      type:'bar',
      data:{{
        labels:{bar_labels},
        datasets:[{{label:'Issues',data:{bar_values},
          backgroundColor:'rgba(239,68,68,0.7)',borderRadius:3}}]
      }},
      options:{{indexAxis:'y',responsive:true,
        plugins:{{legend:{{display:false}},
          tooltip:{{callbacks:{{label:ctx=>` ${{ctx.parsed.x}} issues`}}}}}},
        scales:{{
          x:{{grid:{{color:'#334155'}},ticks:{{color:'#94a3b8'}}}},
          y:{{grid:{{display:false}},ticks:{{color:'#94a3b8',font:{{size:11}}}}}}
        }}
      }}
    }});
  }} catch(e) {{ console.warn('Chart.js error:', e); }}
}}

// ── Table rendering ──────────────────────────────────────────
function esc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}}
function short(s,n){{return s.length>n?s.slice(0,n)+'…':s}}
function loadCell(lm){{
  if(lm===-1||lm===null||lm===undefined)return '<td class="td-load">—</td>';
  const cls=lm>3000?'load-slow':lm>1000?'load-med':'load-ok';
  const txt=lm<1000?lm+' ms':(lm/1000).toFixed(1)+'s';
  return `<td class="td-load ${{cls}}">${{txt}}</td>`;
}}
function statusPill(rc,st){{return `<span class="pill p-${{rc}}">${{st}}</span>`}}
function typeBadge(tp){{const c=(tp||'page').toLowerCase();return `<span class="tt tt-${{c}}">${{tp||'Page'}}</span>`}}
function effortBadge(ef){{
  const m={{'None':'b-none','Low':'b-low','Low-Medium':'b-lowmed','Medium':'b-med','High':'b-high'}};
  return `<span class="bdg ${{m[ef]||'b-med'}}">${{ef}}</span>`;
}}

function renderPage(page) {{
  curPage = page;
  const slice = filtered.slice(page * PAGE_SIZE, (page+1) * PAGE_SIZE);
  document.getElementById('tbody').innerHTML = slice.map(r => `
    <tr class="row-${{r.rc}}">
      <td title="${{esc(r.pu)}}"><a href="${{esc(r.pu)}}" target="_blank">${{esc(short(r.pu,65))}}</a></td>
      <td title="${{esc(r.lu)}}"><a href="${{esc(r.lu)}}" target="_blank">${{esc(short(r.lu,65))}}</a></td>
      <td title="${{esc(r.lt)}}">${{esc(short(r.lt,45))}}</td>
      <td>${{typeBadge(r.tp)}}</td>
      <td>${{statusPill(r.rc,r.st)}}</td>
      <td>${{esc(r.ca)}}</td>
      <td>${{esc(r.dp)}}</td>
      <td>${{effortBadge(r.ef)}}</td>
      ${{loadCell(r.lm)}}
      <td style="font-family:monospace;font-size:11px;color:#7dd3fc;white-space:nowrap">${{esc(r.pi||'N/A')}}</td>
      <td style="font-family:monospace;font-size:11px;color:#93c5fd;white-space:nowrap">${{esc(r.li||'N/A')}}</td>
      <td>${{esc(r.ts)}}</td>
      <td>${{esc(r.rt||'Link')}}</td>
    </tr>`).join('');
  renderPager();
  const total = filtered.length;
  document.getElementById('rowInfo').textContent =
    `${{total.toLocaleString()}} rows · page ${{page+1}} of ${{Math.max(1,Math.ceil(total/PAGE_SIZE))}}`;
  document.getElementById('filteredCount').textContent =
    `${{total.toLocaleString()}} rows in current filter`;
}}

function renderPager() {{
  const total = Math.ceil(filtered.length / PAGE_SIZE);
  const pager = document.getElementById('pager');
  if (total <= 1) {{ pager.innerHTML = ''; return; }}
  let pages = [];
  if (total <= 9) {{ pages = Array.from({{length:total}},(_,i)=>i); }}
  else {{
    pages = [0];
    let lo = Math.max(1, curPage-3), hi = Math.min(total-2, curPage+3);
    if (lo > 1) pages.push('…');
    for (let i = lo; i <= hi; i++) pages.push(i);
    if (hi < total-2) pages.push('…');
    pages.push(total-1);
  }}
  pager.innerHTML =
    `<button onclick="renderPage(${{curPage-1}})" ${{curPage===0?'disabled':''}}>‹ Prev</button>` +
    pages.map(p => p === '…'
      ? `<span class="pginfo">…</span>`
      : `<button class="${{p===curPage?'active':''}}" onclick="renderPage(${{p}})">${{p+1}}</button>`
    ).join('') +
    `<button onclick="renderPage(${{curPage+1}})" ${{curPage>=total-1?'disabled':''}}>Next ›</button>`;
}}

// ── Filters ─────────────────────────────────────────────────
let _searchTimer = null;
function applyFilters() {{ clearTimeout(_searchTimer); _searchTimer = setTimeout(_applyFilters, 120); }}
function _applyFilters() {{
  const q       = document.getElementById('search').value.toLowerCase().trim();
  const st      = document.getElementById('statusFilter').value;
  const tp      = document.getElementById('typeFilter').value;
  const ef      = document.getElementById('effortFilter').value;
  const hide200 = document.getElementById('hide200')?.checked;
  const dom     = (document.getElementById('domainFilter')?.value || '').toLowerCase().trim();
  filtered = ALL_DATA.filter(r => {{
    if (hide200 && r.rc === 'ok') return false;
    if (st  && r.rc !== st)  return false;
    if (tp  && r.tp !== tp)  return false;
    if (ef  && r.ef !== ef)  return false;
    if (dom && !r.lu.toLowerCase().includes(dom)) return false;
    if (q && !(
      r.pu.toLowerCase().includes(q) ||
      r.lu.toLowerCase().includes(q) ||
      r.lt.toLowerCase().includes(q) ||
      (r.pi||'').includes(q) ||
      (r.li||'').includes(q)
    )) return false;
    return true;
  }});
  if (sortCol >= 0) sortFiltered();
  renderPage(0);
  updateCardHighlight();
}}

function resetFilters() {{
  document.getElementById('search').value = '';
  document.getElementById('statusFilter').value = '';
  document.getElementById('typeFilter').value = '';
  document.getElementById('effortFilter').value = '';
  const h200 = document.getElementById('hide200'); if (h200) h200.checked = false;
  const hdom = document.getElementById('domainFilter'); if (hdom) hdom.value = '';
  filtered = ALL_DATA.slice(); sortCol = -1; sortAsc = true;
  document.querySelectorAll('#thead th').forEach(th =>
    th.classList.remove('sort-asc','sort-desc'));
  document.querySelectorAll('.card').forEach(c => c.classList.remove('active'));
  renderPage(0);
}}

function cardFilter(rc) {{
  document.getElementById('statusFilter').value = rc;
  document.getElementById('typeFilter').value = '';
  document.getElementById('effortFilter').value = '';
  document.getElementById('search').value = '';
  _applyFilters();
}}
function typeFilter2(tp) {{
  document.getElementById('typeFilter').value = tp.charAt(0).toUpperCase() + tp.slice(1);
  document.getElementById('statusFilter').value = '';
  document.getElementById('effortFilter').value = '';
  document.getElementById('search').value = '';
  _applyFilters();
}}
function updateCardHighlight() {{
  const st = document.getElementById('statusFilter').value;
  const tp = document.getElementById('typeFilter').value.toLowerCase();
  document.querySelectorAll('.card').forEach(c => c.classList.remove('active'));
  const m = {{'ok':'card-ok','redirect':'card-redirect','broken':'card-broken',
              'servererr':'card-5xx','unkn':'card-err'}};
  if (m[st]) document.getElementById(m[st])?.classList.add('active');
  if (tp === 'internal') document.getElementById('card-int')?.classList.add('active');
  if (tp === 'external') document.getElementById('card-ext')?.classList.add('active');
}}

// ── Sort ─────────────────────────────────────────────────────
const SORT_KEYS = ['pu','lu','lt','tp','st','ca','dp','ef','lm','pi','li','ts','rt'];
function sortBy(col) {{
  if (sortCol === col) {{ sortAsc = !sortAsc; }} else {{ sortCol = col; sortAsc = true; }}
  document.querySelectorAll('#thead th').forEach((th,i) => {{
    th.classList.remove('sort-asc','sort-desc');
    if (i === col) th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
  }});
  sortFiltered();
  renderPage(0);
}}
function sortFiltered() {{
  const key = SORT_KEYS[sortCol];
  filtered.sort((a,b) => {{
    const va = String(a[key]??''), vb = String(b[key]??'');
    const n  = Number(va) - Number(vb);
    const cmp = isNaN(n) ? va.localeCompare(vb) : n;
    return sortAsc ? cmp : -cmp;
  }});
}}

// ── Excel export ─────────────────────────────────────────────
const XL_HEADERS = ['Parent URL','Link URL','Anchor Text','Type','Status',
                    'Category','Depth','Effort','Load Time (ms)','Timestamp'];
const XL_KEYS    = ['pu','lu','lt','tp','st','ca','dp','ef','lm','ts'];
const XL_FILLS   = {{
  ok:       {{patternType:"solid",fgColor:{{rgb:"D4EDDA"}}}},
  redirect: {{patternType:"solid",fgColor:{{rgb:"FFF3CD"}}}},
  broken:   {{patternType:"solid",fgColor:{{rgb:"F8D7DA"}}}},
  warn:     {{patternType:"solid",fgColor:{{rgb:"FFE5CC"}}}},
  servererr:{{patternType:"solid",fgColor:{{rgb:"E2D9F3"}}}},
  unkn:     {{patternType:"solid",fgColor:{{rgb:"E9ECEF"}}}},
}};
function rowsToSheet(data) {{
  const ws_data = [XL_HEADERS,...data.map(r=>XL_KEYS.map(k=>{{
    const v=r[k]??'';
    if(k==='lm') return v===-1?'':Number(v);
    if(k==='dp') return v===''?'':Number(v)||v;
    return String(v);
  }}))];
  const ws = XLSX.utils.aoa_to_sheet(ws_data);
  ws['!cols']=[{{wch:60}},{{wch:60}},{{wch:35}},{{wch:10}},{{wch:12}},
               {{wch:18}},{{wch:7}},{{wch:12}},{{wch:14}},{{wch:24}}];
  data.forEach((r,ri)=>{{
    const fill=XL_FILLS[r.rc];
    if(!fill)return;
    for(let ci=0;ci<XL_HEADERS.length;ci++){{
      const addr=XLSX.utils.encode_cell({{r:ri+1,c:ci}});
      if(!ws[addr])ws[addr]={{t:'s',v:''}};
      ws[addr].s={{fill}};
    }}
  }});
  return ws;
}}
function buildWorkbook(data,sheetName) {{
  const wb=XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb,rowsToSheet(data),sheetName);
  const summary=[
    ['Broken Link Report'],['Site','{base_url}'],
    ['Run Date','{run_date}'],['Duration','{run_dur}'],[''],
    ['Metric','Count'],
    ['Parent Pages',{total_pages}],['Total Links',{total_links}],
    ['200 OK',{ok_links}],['Redirects (3xx)',{redirects}],
    ['Broken (404/410)',{broken}],['Server Errors (5xx)',{server_errors}],
    ['Timeout / Error',{errors}],['Internal',{internal}],['External',{external}],
  ];
  const ws2=XLSX.utils.aoa_to_sheet(summary);
  ws2['!cols']=[{{wch:24}},{{wch:40}}];
  XLSX.utils.book_append_sheet(wb,ws2,'Summary');
  return wb;
}}
function exportExcelFull()     {{ XLSX.writeFile(buildWorkbook(ALL_DATA,'All Links'),'broken_links_full_report.xlsx'); }}
function exportExcelFiltered() {{ XLSX.writeFile(buildWorkbook(filtered,'Filtered'),'broken_links_filtered.xlsx'); }}

// ── CSV export ────────────────────────────────────────────────
function rowsToCsv(data) {{
  const h=['Parent URL','Link URL','Anchor Text','Type','Status',
           'Category','Depth','Effort','Load Time (ms)','Timestamp'];
  const k=['pu','lu','lt','tp','st','ca','dp','ef','lm','ts'];
  const esc2=v=>{{const s=v===null||v===undefined?'':String(v);
    return s.includes(',')||s.includes('"')||s.includes('\\n')
      ?'"'+s.replace(/"/g,'""')+'"':s;}};
  return [h.join(','),...data.map(r=>k.map(k2=>{{
    const v=r[k2]??'';return esc2(k2==='lm'&&v===-1?'':v);
  }}).join(','))].join('\\n');
}}
function downloadCsv(content,filename) {{
  const blob=new Blob(['\\uFEFF'+content],{{type:'text/csv;charset=utf-8;'}});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;a.download=filename;a.click();URL.revokeObjectURL(url);
}}
function exportCsvFull()     {{ downloadCsv(rowsToCsv(ALL_DATA),'broken_links_full_report.csv'); }}
function exportCsvFiltered() {{ downloadCsv(rowsToCsv(filtered),'broken_links_filtered.csv'); }}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def _row_class(status) -> str:
    s = str(status)
    if s == "200":         return "ok"
    if s.startswith("3"):  return "redirect"
    if s in ("404","410"): return "broken"
    if s.startswith("4"):  return "warn"
    if s.startswith("5"):  return "servererr"
    return "unkn"


def _skip_upload(name: str) -> bool:
    """Files that only repeat data another uploaded file already holds."""
    return (
        name == "live_results.jsonl"      # working log; every row is in the CSV
        or name.startswith("report_data_")  # the HTML embeds the same rows
        or (name.startswith("report_") and name.endswith(".html"))  # same bytes as index.html
    )


def upload_reports(output_dir: Path, bucket: str, prefix: str, s3=None) -> list[str]:
    """Upload the report files in output_dir to s3://bucket/prefix/ and return
    the keys.

    Nothing is lost to keep the upload small: duplicates are skipped (see
    _skip_upload), the CSV is stored gzipped as <name>.csv.gz, and HTML pages
    are gzipped with Content-Encoding so a browser still opens them directly.
    """
    if s3 is None:
        import boto3
        s3 = boto3.client("s3")
    keys = []
    with tempfile.TemporaryDirectory() as tmp:
        for path in sorted(p for p in output_dir.rglob("*") if p.is_file()):
            if _skip_upload(path.name):
                continue
            key = f"{prefix}/{path.relative_to(output_dir).as_posix()}"
            src, extra = path, None
            if path.suffix in (".csv", ".html"):
                src = Path(tmp) / (path.name + (".gz" if path.suffix == ".csv" else ""))
                with open(path, "rb") as fin, gzip.open(src, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                if path.suffix == ".csv":
                    key += ".gz"
                    extra = {"ContentType": "application/gzip"}
                else:
                    extra = {"ContentType": "text/html; charset=utf-8",
                             "ContentEncoding": "gzip"}
            if extra:
                s3.upload_file(str(src), bucket, key, ExtraArgs=extra)
            else:
                s3.upload_file(str(src), bucket, key)
            keys.append(key)
    log.info("Uploaded %d file(s) → s3://%s/%s/", len(keys), bucket, prefix)
    return keys


# ── Google Drive upload (disabled, kept for possible future use) ──
# Uploads the .xlsx to a Drive folder and converts it to a native Google
# Sheet. Not called from main() right now; see the note there to re-enable.

# _XLSX_MIME  = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# _SHEET_MIME = "application/vnd.google-apps.spreadsheet"
#
#
# def drive_upload_configured(credentials_json: str, folder_id: str) -> bool:
#     """False (skip quietly) while the credential is unset or the "{}"
#     placeholder. A real credential with no folder id is a misconfiguration
#     and fails loudly instead of silently never uploading."""
#     creds = (credentials_json or "").strip()
#     if not creds or json.loads(creds) == {}:
#         return False
#     if not folder_id:
#         raise ValueError("GOOGLE_DRIVE_CREDENTIALS is set but "
#                          "GOOGLE_DRIVE_FOLDER_ID is empty")
#     return True
#
#
# def upload_sheet_to_drive(
#     xlsx_path: str, name: str, folder_id: str, credentials_json: str,
#     service=None,
# ) -> str:
#     """Upload xlsx_path into the Drive folder and convert it to a native
#     Google Sheet. Returns the link to the new Sheet. `service` is injectable
#     for tests."""
#     if service is None:
#         from google.oauth2 import service_account
#         from googleapiclient.discovery import build
#         creds = service_account.Credentials.from_service_account_info(
#             json.loads(credentials_json),
#             scopes=["https://www.googleapis.com/auth/drive.file"],
#         )
#         service = build("drive", "v3", credentials=creds, cache_discovery=False)
#     from googleapiclient.http import MediaFileUpload
#     media = MediaFileUpload(xlsx_path, mimetype=_XLSX_MIME, resumable=True)
#     created = service.files().create(
#         body={"name": name, "parents": [folder_id], "mimeType": _SHEET_MIME},
#         media_body=media, fields="id,webViewLink", supportsAllDrives=True,
#     ).execute()
#     log.info("Uploaded Google Sheet '%s' → %s", name, created.get("webViewLink"))
#     return created.get("webViewLink") or created.get("id", "")


def crawl_failed(results: list) -> bool:
    """True when not a single crawled page answered 2xx, i.e. the site was
    unreachable or the start URL is wrong. A normal run that merely finds
    broken links is not a failure."""
    return not any(
        r.get("link_type") == "Page" and str(r.get("status", "")).startswith("2")
        for r in results
    )


async def main() -> None:
    start_time = time.time()
    start_dt   = now_est().strftime("%Y-%m-%d %H:%M:%S")
    log.info("Starting — BASE_URL=%s", CONFIG["BASE_URL"])
    log.info(
        "Config: MAX_DEPTH=%d  MAX_PAGES=%s  MAX_QUEUE=%d  "
        "CONCURRENCY=%d  TIMEOUT=%ds  HTML_MAX_ROWS=%s",
        CONFIG["MAX_DEPTH"],
        "unlimited" if CONFIG["MAX_PAGES"] == 0 else CONFIG["MAX_PAGES"],
        CONFIG["MAX_QUEUE"],
        CONFIG["CONCURRENCY"],
        CONFIG["TIMEOUT"],
        "unlimited" if CONFIG["HTML_MAX_ROWS"] == 0 else CONFIG["HTML_MAX_ROWS"],
    )

    target_pages = CONFIG.get("TARGET_PAGES", [])
    scan_mode    = "targeted" if target_pages else "full"

    if scan_mode == "targeted":
        log.info("Mode: TARGETED — %d page(s) specified", len(target_pages))
        results          = await targeted_crawl(target_pages)
        dropped_urls     = []
        skipped_snapshot = {}
    else:
        log.info("Mode: FULL CRAWL — starting from %s", CONFIG["BASE_URL"])
        results, dropped_urls, skipped_snapshot = await crawl()

    output_dir = Path(CONFIG["OUTPUT_DIR"])
    output_dir.mkdir(parents=True, exist_ok=True)

    if not results:
        # Exit non-zero so ECS marks the task failed (and the failure alert
        # fires) instead of an unreachable site looking like a clean run.
        log.error("No results collected. Check BASE_URL and network access.")
        raise SystemExit("No results collected. Check BASE_URL and network access.")

    elapsed  = time.time() - start_time
    run_date = now_est().strftime("%Y-%m-%d %H:%M:%S")
    ts_tag   = now_est().strftime("%Y%m%d_%H%M%S")
    label    = CONFIG.get("SCAN_LABEL", "")
    safe_lbl = label.replace(" ", "_").replace("/", "_")[:30] if label else ""
    file_tag = f"{ts_tag}_{safe_lbl}" if safe_lbl else ts_tag

    csv_path           = str(output_dir / f"broken_links_{file_tag}.csv")
    xlsx_path          = str(output_dir / f"broken_links_{file_tag}.xlsx")
    html_path          = str(output_dir / "index.html")
    html_ts_path       = str(output_dir / f"report_{file_tag}.html")
    data_json_filename = f"report_data_{file_tag}.json"
    data_json_path     = str(output_dir / data_json_filename)

    # ── Deduplicate: remove duplicate (page_url, link_url) pairs.
    #    Fixes table duplication caused by sort/order URL variants.
    _seen_pairs: set = set()
    deduped: list = []
    for r in results:
        pair = (r.get("page_url",""), r.get("link_url",""))
        if pair not in _seen_pairs:
            _seen_pairs.add(pair)
            deduped.append(r)
    if len(deduped) < len(results):
        log.info(
            "Deduplication: %d rows -> %d  (removed %d duplicate page+link pairs)",
            len(results), len(deduped), len(results) - len(deduped),
        )
    results = deduped

    # ── IP enrichment ──
    await enrich_with_ips(results)

    # ── CSV (full data) ──
    write_csv(results, csv_path)

    # ── dropped_pages.txt ──
    seen_dropped  = set()
    unique_dropped = []
    for u in dropped_urls:
        if u not in seen_dropped:
            seen_dropped.add(u)
            unique_dropped.append(u)
    if unique_dropped:
        dropped_path = str(output_dir / "dropped_pages.txt")
        with open(dropped_path, "w", encoding="utf-8") as dp:
            dp.write("# Pages dropped due to MAX_QUEUE cap\n")
            dp.write(f"# Total: {len(unique_dropped)} unique URLs\n")
            dp.write(",".join(unique_dropped) + "\n")
            for u in unique_dropped:
                dp.write(u + "\n")
        log.info(
            "dropped_pages.txt → %s  (%d unique URLs)",
            dropped_path, len(unique_dropped),
        )

    # ── Excel (non-200 rows only + pivot) ──
    write_excel(results, xlsx_path, start_dt=start_dt, end_dt=run_date,
                duration_str=fmt_duration(elapsed))

    # ── JSON data file for HTML report ──
    # Priority: all non-200 rows first, then 200 OK up to HTML_MAX_ROWS
    non_ok  = [r for r in results if str(r.get("status","")) != "200"]
    ok_rows = [r for r in results if str(r.get("status","")) == "200"]
    max_rows = CONFIG.get("HTML_MAX_ROWS", 100000)

    if max_rows == 0 or (len(non_ok) + len(ok_rows)) <= max_rows:
        display_results = results
        truncated       = False
        truncated_count = 0
    else:
        remaining       = max(0, max_rows - len(non_ok))
        display_results = non_ok + ok_rows[:remaining]
        truncated       = True
        truncated_count = len(results) - len(display_results)
        log.warning(
            "HTML_MAX_ROWS=%d: report limited to %d rows (%d omitted, all non-200 kept).",
            max_rows, len(display_results), truncated_count,
        )

    js_rows = []
    for r in display_results:
        s  = str(r.get("status",""))
        lm = r.get("load_ms", -1)
        js_rows.append({
            "pu": r["page_url"],
            "lu": r["link_url"],
            "lt": r.get("link_text","")[:120],
            "tp": r.get("link_type",""),
            "st": s,
            "fu": r.get("final_url",""),
            "lm": lm if lm is not None else -1,
            "dp": r.get("depth",""),
            "ef": r.get("effort",""),
            "ca": r.get("category",""),
            "pi": r.get("page_ip","N/A"),
            "li": r.get("link_ip","N/A"),
            "rc": _row_class(r["status"]),
            "ts": str(r.get("timestamp",""))[:22],
            "rt": r.get("resource_type", "Link"),
        })

    with open(data_json_path, "w", encoding="utf-8") as jf:
        json.dump(js_rows, jf, ensure_ascii=False)
    log.info(
        "Data JSON written → %s  (%d rows)", data_json_path, len(js_rows)
    )

    # ── HTML shell ──
    # Row data is always embedded directly, at any size, so the report
    # opens with a plain double-click and can be emailed/shared as one
    # file - no server, no paired JSON file to lose track of. This used
    # to cap out at 50,000 rows and fall back to fetch(DATA_FILE), which
    # needs the file served over HTTP - but that fallback is exactly what
    # breaks a file:// double-click, so every crawl now inlines regardless
    # of row count. build_html_report() still accepts inline_data=None as
    # a fallback for callers that want the old fetch()-based page.
    html = build_html_report(
        results,
        csv_path,
        elapsed,
        scan_mode=scan_mode,
        target_pages=target_pages,
        dropped_urls=unique_dropped,
        skipped_snapshot=skipped_snapshot,
        data_json_filename=data_json_filename,
        truncated=truncated,
        truncated_count=truncated_count,
        inline_data=js_rows,
    )
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("HTML report written → %s", html_path)

    # Save a timestamped copy so multiple scans coexist in the reports folder
    with open(html_ts_path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("Timestamped copy    → %s", html_ts_path)

    # ── Skipped URLs report ──
    if skipped_snapshot:
        skipped_path = str(output_dir / "skipped_urls.html")
        with open(skipped_path, "w", encoding="utf-8") as f:
            f.write(build_skipped_html(
                skipped_snapshot, CONFIG["BASE_URL"], run_date, skipped_path
            ))
        log.info("skipped_urls.html written → %s", skipped_path)

    if CONFIG["REPORT_BUCKET"]:
        upload_reports(output_dir, CONFIG["REPORT_BUCKET"], f"{safe_lbl or 'unlabeled'}/{ts_tag}")

    # Google Drive upload is switched off (decision: reports go to S3 only).
    # To bring it back, uncomment this block, the Drive helpers above main(),
    # the two GOOGLE_DRIVE_* CONFIG keys, the `if drive_error:` raise at the
    # end of main(), and the google-* lines in requirements.txt.
    # Runs after S3 so the report is safely stored even if Drive fails. A
    # Drive failure is raised at the very end so the task exits non-zero.
    # drive_error = None
    # try:
    #     if (os.path.exists(xlsx_path) and drive_upload_configured(
    #             CONFIG["GOOGLE_DRIVE_CREDENTIALS"], CONFIG["GOOGLE_DRIVE_FOLDER_ID"])):
    #         site = label or urlparse(CONFIG["BASE_URL"]).netloc
    #         upload_sheet_to_drive(
    #             xlsx_path, f"Broken Link Report - {site} - {run_date[:10]}",
    #             CONFIG["GOOGLE_DRIVE_FOLDER_ID"], CONFIG["GOOGLE_DRIVE_CREDENTIALS"],
    #         )
    #     else:
    #         log.info("Google Drive upload skipped (not configured).")
    # except Exception as exc:
    #     drive_error = exc
    #     log.error("Google Drive upload failed: %s", exc)

    # ── Terminal summary ──
    broken      = sum(1 for r in results if str(r["status"]) in ("404","410","451"))
    ok_cnt      = sum(1 for r in results if str(r["status"]) == "200")
    mode_str    = (
        f"TARGETED ({len(target_pages)} pages)"
        if scan_mode == "targeted" else "FULL CRAWL"
    )
    pages_crawled = len({r["page_url"] for r in results})

    print(f"\n{'─'*62}")
    print(f"  SCAN SUMMARY  [{mode_str}]")
    if label:
        print(f"  Label      : {label}")
    print(f"  Base URL   : {CONFIG['BASE_URL']}")
    print(f"  Run Date   : {run_date}")
    print(f"  Pages      : {pages_crawled:,}")
    print(f"  Total links: {len(results):,}")
    print(f"  200 OK     : {ok_cnt:,}")
    print(f"  Broken     : {broken:,}")
    print(f"  Duration   : {fmt_duration(elapsed)}")
    if unique_dropped:
        print(f"  ⚠️  Queue dropped : {len(unique_dropped):,} pages not crawled")
    if truncated:
        print(
            f"  ⚠️  HTML truncated: {truncated_count:,} rows omitted "
            f"(HTML_MAX_ROWS={max_rows:,})"
        )
    print(f"  Reports    : {csv_path}")
    print(f"             : {html_path}")
    print(f"             : {xlsx_path}")
    print(f"             : {data_json_path}")
    html_mb = len(html.encode("utf-8")) / (1024 * 1024)
    print(f"\n  ⚡ {html_path} opens directly (double-click, no server) "
          f"and is safe to email/share as-is.")
    if html_mb >= 20:
        print(f"     ({html_mb:,.0f} MB - large, but still a single "
              f"double-clickable file. May take a few seconds to open.)")
        print(f"     Then open http://localhost:8088")
    print(f"{'─'*62}\n")

    # if drive_error:
    #     raise SystemExit(f"Google Drive upload failed: {drive_error}")

    # Raised last, after the report is written and uploaded, so the evidence is
    # kept. Non-zero exit makes ECS mark the task failed and fires the alert.
    if crawl_failed(results):
        raise SystemExit("Crawl failed: no page returned a successful response. "
                         "Check BASE_URL and network access.")


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT — cross-platform asyncio setup
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Windows: ProactorEventLoop supports subprocess and is the default in 3.8+
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
