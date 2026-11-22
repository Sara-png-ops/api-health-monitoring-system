"""Health-checking logic for the API Health Monitoring System.

Every check sends a real HTTP GET request. Nothing in this module fabricates
responses, timings or statistics.

How a check is classified
-------------------------
* healthy   - the final HTTP status code is 200-399 (redirects are followed).
* unhealthy - the server answered with a 4xx or 5xx status code.
* error     - no HTTP response was received: timeout, DNS or connection
              failure, SSL problem, redirect loop or an invalid URL.

How response time is measured
-----------------------------
Milliseconds from just before the request is sent until the response headers
arrive. This includes DNS lookup, connection setup, TLS handshake and server
processing, but not the download of the response body (the body is never read,
so large responses do not slow a check down). Failed requests record no
response time because there is no complete response to time.
"""

from __future__ import annotations

import csv
import io
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlparse

import requests

DEFAULT_TIMEOUT_SECONDS = 10
MAX_PARALLEL_CHECKS = 5
USER_AGENT = "api-health-monitoring-system/1.0"

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
ERROR = "error"
RESULT_LABELS = {HEALTHY: "Healthy", UNHEALTHY: "Unhealthy", ERROR: "Error"}

# URLs are stored in plain text, so URLs that carry secrets are rejected.
SENSITIVE_QUERY_KEYS = {
    "key", "api_key", "apikey", "api-key", "token", "access_token", "auth",
    "authorization", "secret", "client_secret", "password", "passwd", "pwd",
    "signature", "sig",
}

SAMPLE_ENDPOINTS = [
    ("Sample: JSONPlaceholder post", "https://jsonplaceholder.typicode.com/posts/1"),
    ("Sample: GitHub API root", "https://api.github.com"),
    ("Sample: Open-Meteo forecast", "https://api.open-meteo.com/v1/forecast?latitude=25.2&longitude=55.27&current=temperature_2m"),
    ("Sample: httpbin always 200", "https://httpbin.org/status/200"),
    ("Sample: httpbin always 500 (intentionally failing)", "https://httpbin.org/status/500"),
]

REPORT_COLUMNS = [
    "Endpoint name", "Endpoint URL", "Checked at (UTC)", "HTTP status",
    "Response time (ms)", "Result", "Error detail",
]


class InvalidURLError(ValueError):
    """The text entered is not a usable HTTP or HTTPS URL."""


@dataclass
class CheckResult:
    url: str
    checked_at: str
    status_code: Optional[int]
    response_time_ms: Optional[float]
    result: str
    error_message: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_clock = time.perf_counter  # replaced in tests to make timing deterministic


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_timestamp(iso_timestamp: Optional[str]) -> str:
    """Turn ``2026-11-22T14:50:13Z`` into ``2026-11-22 14:50:13``."""
    if not iso_timestamp:
        return ""
    return iso_timestamp.replace("T", " ").replace("Z", "")


def validate_url(url: str) -> str:
    """Return the cleaned URL or raise ``InvalidURLError`` with a clear reason."""
    if not isinstance(url, str) or not url.strip():
        raise InvalidURLError("Enter an endpoint URL.")
    candidate = url.strip()
    if any(char.isspace() for char in candidate):
        raise InvalidURLError("The URL must not contain spaces.")

    try:
        parsed = urlparse(candidate)
        port = parsed.port  # raises ValueError for an invalid port
    except ValueError as exc:
        raise InvalidURLError("The URL is malformed (check the host and port).") from exc

    if parsed.scheme.lower() not in ("http", "https"):
        raise InvalidURLError("The URL must start with http:// or https://.")
    host = parsed.hostname
    if not host or not all(c.isalnum() or c in ".-_:" for c in host):
        raise InvalidURLError("The URL does not contain a valid host name.")
    if port is not None and not 0 < port < 65536:
        raise InvalidURLError("The URL contains an invalid port number.")
    if parsed.username or parsed.password:
        raise InvalidURLError(
            "Do not put a username or password in the URL. URLs are stored in plain text."
        )
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in SENSITIVE_QUERY_KEYS:
            raise InvalidURLError(
                f"The query parameter '{key}' looks like a secret. URLs are stored in plain text, "
                "so use an endpoint that does not need one."
            )
    return candidate


def default_endpoint_name(url: str) -> str:
    """Build a readable name such as ``api.github.com/repos`` from a URL."""
    parsed = urlparse(url)
    name = (parsed.hostname or url) + parsed.path.rstrip("/")
    return name[:60]


def classify_status(status_code: int) -> str:
    return HEALTHY if 200 <= status_code < 400 else UNHEALTHY


# --------------------------------------------------------------------------- #
# Health checks
# --------------------------------------------------------------------------- #
def check_endpoint(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                   http_get: Optional[Callable] = None) -> CheckResult:
    """Send one real GET request and describe the outcome. Never raises."""
    try:
        valid_url = validate_url(url)
    except InvalidURLError as exc:
        return CheckResult(url, utc_now_iso(), None, None, ERROR, str(exc))

    http_get = http_get or requests.get
    start = _clock()
    try:
        response = http_get(
            valid_url,
            timeout=timeout,
            allow_redirects=True,
            stream=True,  # read headers only; never download the body
            headers={"User-Agent": USER_AGENT},
        )
    except requests.exceptions.SSLError:
        return _error(url, "SSL certificate or handshake error.")
    except requests.exceptions.Timeout:
        return _error(url, f"Request timed out after {timeout:g} seconds.")
    except requests.exceptions.ConnectionError:
        return _error(url, "Could not connect (DNS failure, refused connection or no network).")
    except requests.exceptions.TooManyRedirects:
        return _error(url, "Too many redirects.")
    except requests.exceptions.RequestException as exc:
        return _error(url, f"Request failed: {type(exc).__name__}.")
    except Exception as exc:  # defensive: a check must never crash the dashboard
        return _error(url, f"Unexpected error: {type(exc).__name__}.")

    elapsed_ms = round((_clock() - start) * 1000, 1)
    try:
        status = int(response.status_code)
        result = classify_status(status)
        message = None
        if result == UNHEALTHY:
            reason = getattr(response, "reason", None)
            message = f"HTTP {status}" + (f" {reason}" if isinstance(reason, str) and reason else "")
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    return CheckResult(url, utc_now_iso(), status, elapsed_ms, result, message)


def _error(url: str, message: str) -> CheckResult:
    return CheckResult(url, utc_now_iso(), None, None, ERROR, message)


def check_endpoints(urls: list[str], timeout: float = DEFAULT_TIMEOUT_SECONDS,
                    http_get: Optional[Callable] = None) -> list[CheckResult]:
    """Check several URLs in parallel (at most five at once). Order is preserved."""
    if not urls:
        return []
    workers = min(MAX_PARALLEL_CHECKS, len(urls))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda u: check_endpoint(u, timeout, http_get), urls))


# --------------------------------------------------------------------------- #
# Summaries and reports (built from stored rows only)
# --------------------------------------------------------------------------- #
def summarize_latest(latest_rows: list[dict]) -> dict:
    """Count each endpoint's most recent result. ``latest_rows`` come from the database."""
    counts = {"registered": len(latest_rows), "checked": 0,
              HEALTHY: 0, UNHEALTHY: 0, ERROR: 0, "unchecked": 0}
    for row in latest_rows:
        result = row.get("result")
        if result in (HEALTHY, UNHEALTHY, ERROR):
            counts["checked"] += 1
            counts[result] += 1
        else:
            counts["unchecked"] += 1
    return counts


def _safe_cell(value) -> str:
    """Stop spreadsheet programs from running text as a formula (CSV injection)."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def build_csv_report(rows: list[dict]) -> str:
    """Create a CSV report from stored check rows."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(REPORT_COLUMNS)
    for row in rows:
        writer.writerow([
            _safe_cell(row.get("name")),
            _safe_cell(row.get("url")),
            _safe_cell(format_timestamp(row.get("checked_at"))),
            _safe_cell(row.get("status_code")),
            _safe_cell(row.get("response_time_ms")),
            _safe_cell(RESULT_LABELS.get(row.get("result"), row.get("result"))),
            _safe_cell(row.get("error_message")),
        ])
    return buffer.getvalue()
