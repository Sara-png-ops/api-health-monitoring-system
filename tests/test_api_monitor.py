"""Tests for api_monitor.py and database.py.

Most tests mock the HTTP layer. The "local server" tests at the bottom send
real requests to a throwaway server on 127.0.0.1, so no external API is used.
"""

import csv
import io
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_monitor as am  # noqa: E402
from database import Database, DuplicateEndpointError  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def fake_response(status=200, reason="OK"):
    response = MagicMock()
    response.status_code = status
    response.reason = reason
    return response


def fake_get(response=None, error=None):
    """Build a stand-in for requests.get that records its calls."""
    def _get(url, **kwargs):
        _get.calls.append((url, kwargs))
        if error is not None:
            raise error
        return response
    _get.calls = []
    return _get


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


# --------------------------------------------------------------------------- #
# URL validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "https://api.github.com",
    "http://example.com/health",
    "https://example.com:8443/v1/status?verbose=true",
    "http://localhost:8000/ping",
    "http://127.0.0.1:5000",
    "  https://example.com/  ",
    "https://sub-domain.example.co.uk/a/b#frag",
])
def test_validate_url_accepts_valid_urls(url):
    assert am.validate_url(url) == url.strip()


@pytest.mark.parametrize("url,fragment", [
    ("", "Enter"),
    ("   ", "Enter"),
    (None, "Enter"),
    (42, "Enter"),
    ("example.com", "http://"),
    ("ftp://example.com", "http://"),
    ("javascript:alert(1)", "http://"),
    ("https://", "host"),
    ("http://exa mple.com", "spaces"),
    ("https://example.com:99999", "port"),
    ("https://example.com:abc", "malformed"),
    ("https://user:pass@example.com", "username"),
    ("https://example.com/data?api_key=abc123", "secret"),
    ("https://example.com/data?x=1&Token=abc", "secret"),
    ("https://example.com/data?password=hunter2", "secret"),
])
def test_validate_url_rejects_bad_urls(url, fragment):
    with pytest.raises(am.InvalidURLError) as info:
        am.validate_url(url)
    assert fragment.lower() in str(info.value).lower()


def test_default_endpoint_name():
    assert am.default_endpoint_name("https://api.github.com/repos/") == "api.github.com/repos"
    assert len(am.default_endpoint_name("https://example.com/" + "a" * 100)) == 60


def test_sample_endpoints_are_valid_and_unique():
    urls = [url for _, url in am.SAMPLE_ENDPOINTS]
    assert len(urls) == len(set(urls)) >= 3
    for name, url in am.SAMPLE_ENDPOINTS:
        assert name.startswith("Sample")
        assert am.validate_url(url) == url


# --------------------------------------------------------------------------- #
# Health checks (mocked HTTP)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [200, 201, 204, 301, 399])
def test_healthy_statuses(status):
    result = am.check_endpoint("https://example.com", http_get=fake_get(fake_response(status)))
    assert result.result == am.HEALTHY
    assert result.status_code == status
    assert result.error_message is None


@pytest.mark.parametrize("status,reason", [(400, "Bad Request"), (404, "Not Found"), (500, "Internal Server Error"), (503, "Service Unavailable")])
def test_http_errors_are_unhealthy(status, reason):
    result = am.check_endpoint("https://example.com", http_get=fake_get(fake_response(status, reason)))
    assert result.result == am.UNHEALTHY
    assert result.status_code == status
    assert result.error_message == f"HTTP {status} {reason}"
    assert result.response_time_ms is not None


def test_unhealthy_without_reason_phrase():
    result = am.check_endpoint("https://example.com", http_get=fake_get(fake_response(500, "")))
    assert result.error_message == "HTTP 500"


def test_response_time_is_measured_in_milliseconds(monkeypatch):
    ticks = iter([10.0, 10.2505])
    monkeypatch.setattr(am, "_clock", lambda: next(ticks))
    result = am.check_endpoint("https://example.com", http_get=fake_get(fake_response(200)))
    assert result.response_time_ms == pytest.approx(250.5, abs=0.1)


def test_request_uses_timeout_get_and_closes_response():
    response = fake_response(200)
    getter = fake_get(response)
    am.check_endpoint("https://example.com/x", timeout=3, http_get=getter)
    url, kwargs = getter.calls[0]
    assert url == "https://example.com/x"
    assert kwargs["timeout"] == 3
    assert kwargs["stream"] is True
    assert "User-Agent" in kwargs["headers"]
    assert "Authorization" not in kwargs["headers"]
    response.close.assert_called_once()


def test_result_has_utc_timestamp():
    result = am.check_endpoint("https://example.com", http_get=fake_get(fake_response(200)))
    assert len(result.checked_at) == 20 and result.checked_at.endswith("Z")


@pytest.mark.parametrize("exception,fragment", [
    (requests.exceptions.ConnectTimeout("t"), "timed out"),
    (requests.exceptions.ReadTimeout("t"), "timed out"),
    (requests.exceptions.ConnectionError("dns"), "Could not connect"),
    (requests.exceptions.SSLError("bad cert"), "SSL"),
    (requests.exceptions.TooManyRedirects("loop"), "redirects"),
    (requests.exceptions.InvalidURL("bad"), "InvalidURL"),
    (requests.exceptions.RequestException("x"), "RequestException"),
    (RuntimeError("boom"), "Unexpected error"),
])
def test_failures_become_error_results(exception, fragment):
    result = am.check_endpoint("https://example.com", http_get=fake_get(error=exception))
    assert result.result == am.ERROR
    assert result.status_code is None
    assert result.response_time_ms is None
    assert fragment in result.error_message


def test_invalid_url_returns_error_without_sending_request():
    getter = fake_get(fake_response(200))
    result = am.check_endpoint("not-a-url", http_get=getter)
    assert result.result == am.ERROR
    assert "http://" in result.error_message
    assert getter.calls == []


def test_secret_in_url_is_never_requested():
    getter = fake_get(fake_response(200))
    result = am.check_endpoint("https://example.com/?api_key=abc", http_get=getter)
    assert result.result == am.ERROR and getter.calls == []


def test_check_endpoints_preserves_order_and_mixes_outcomes():
    def getter(url, **kwargs):
        if url.endswith("/ok"):
            return fake_response(200)
        if url.endswith("/bad"):
            return fake_response(500, "Server Error")
        raise requests.exceptions.ConnectionError("down")
    urls = ["https://x.test/ok", "https://x.test/bad", "https://x.test/down", "https://x.test/ok"]
    results = am.check_endpoints(urls, http_get=getter)
    assert [r.url for r in results] == urls
    assert [r.result for r in results] == [am.HEALTHY, am.UNHEALTHY, am.ERROR, am.HEALTHY]


def test_check_endpoints_empty_list():
    assert am.check_endpoints([]) == []


# --------------------------------------------------------------------------- #
# Summaries and CSV report
# --------------------------------------------------------------------------- #
def test_summarize_latest():
    rows = [{"result": "healthy"}, {"result": "healthy"}, {"result": "unhealthy"},
            {"result": "error"}, {"result": None}]
    counts = am.summarize_latest(rows)
    assert counts == {"registered": 5, "checked": 4, "healthy": 2, "unhealthy": 1, "error": 1, "unchecked": 1}
    assert am.summarize_latest([])["registered"] == 0


def parse_csv(text):
    return list(csv.reader(io.StringIO(text)))


def test_csv_report_contains_real_values():
    rows = [{"name": "API", "url": "https://example.com", "checked_at": "2026-11-22T10:15:30Z",
             "status_code": 200, "response_time_ms": 123.4, "result": "healthy", "error_message": None}]
    parsed = parse_csv(am.build_csv_report(rows))
    assert parsed[0] == am.REPORT_COLUMNS
    assert parsed[1] == ["API", "https://example.com", "2026-11-22 10:15:30", "200", "123.4", "Healthy", ""]


def test_csv_report_with_no_rows_has_only_header():
    assert parse_csv(am.build_csv_report([])) == [am.REPORT_COLUMNS]


def test_csv_report_blocks_formula_injection():
    rows = [{"name": "=HYPERLINK(\"http://evil\")", "url": "https://e.com", "checked_at": "2026-01-01T00:00:00Z",
             "status_code": None, "response_time_ms": None, "result": "error", "error_message": "+cmd"}]
    parsed = parse_csv(am.build_csv_report(rows))
    assert parsed[1][0].startswith("'=")
    assert parsed[1][6] == "'+cmd"


def test_csv_report_handles_commas_and_quotes():
    rows = [{"name": 'Name, with "quotes"', "url": "https://e.com", "checked_at": "2026-01-01T00:00:00Z",
             "status_code": 500, "response_time_ms": 1.0, "result": "unhealthy", "error_message": "HTTP 500"}]
    assert parse_csv(am.build_csv_report(rows))[1][0] == 'Name, with "quotes"'


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def test_add_and_list_endpoints(db):
    first = db.add_endpoint("Zeta", "https://z.test")
    second = db.add_endpoint("alpha", "https://a.test")
    endpoints = db.list_endpoints()
    assert [e["name"] for e in endpoints] == ["alpha", "Zeta"]  # case-insensitive order
    assert {e["id"] for e in endpoints} == {first, second}


def test_duplicate_url_is_rejected(db):
    db.add_endpoint("One", "https://dup.test")
    with pytest.raises(DuplicateEndpointError):
        db.add_endpoint("Two", "https://dup.test")
    assert len(db.list_endpoints()) == 1


def test_remove_endpoint_deletes_its_history_only(db):
    keep, drop = db.add_endpoint("Keep", "https://k.test"), db.add_endpoint("Drop", "https://d.test")
    db.save_check(keep, "2026-01-01T00:00:00Z", 200, 10.0, "healthy")
    db.save_check(drop, "2026-01-01T00:00:00Z", 500, 20.0, "unhealthy", "HTTP 500")
    assert db.remove_endpoint(drop) is True
    assert [e["id"] for e in db.list_endpoints()] == [keep]
    assert [h["endpoint_id"] for h in db.get_history()] == [keep]
    assert db.remove_endpoint(drop) is False


def test_save_and_read_check_round_trip(db):
    eid = db.add_endpoint("API", "https://api.test")
    db.save_check(eid, "2026-11-22T10:00:00Z", 503, 87.5, "unhealthy", "HTTP 503 Service Unavailable")
    row = db.get_history()[0]
    assert (row["name"], row["url"], row["status_code"], row["response_time_ms"], row["result"]) == (
        "API", "https://api.test", 503, 87.5, "unhealthy")
    assert row["error_message"] == "HTTP 503 Service Unavailable"


def test_error_check_stores_nulls(db):
    eid = db.add_endpoint("API", "https://api.test")
    db.save_check(eid, "2026-11-22T10:00:00Z", None, None, "error", "Request timed out after 10 seconds.")
    row = db.get_history()[0]
    assert row["status_code"] is None and row["response_time_ms"] is None


def test_history_is_newest_first_filterable_and_limited(db):
    a, b = db.add_endpoint("A", "https://a.test"), db.add_endpoint("B", "https://b.test")
    for minute in range(5):
        db.save_check(a, f"2026-01-01T00:0{minute}:00Z", 200, float(minute), "healthy")
    db.save_check(b, "2026-01-01T00:09:00Z", 200, 99.0, "healthy")
    all_rows = db.get_history()
    assert len(all_rows) == 6 and all_rows[0]["endpoint_id"] == b
    only_a = db.get_history(endpoint_id=a)
    assert [r["response_time_ms"] for r in only_a] == [4.0, 3.0, 2.0, 1.0, 0.0]
    assert len(db.get_history(limit=2)) == 2
    assert db.get_history(endpoint_id=999) == []


def test_latest_checks_returns_most_recent_and_unchecked(db):
    a, b = db.add_endpoint("A", "https://a.test"), db.add_endpoint("B", "https://b.test")
    db.save_check(a, "2026-01-01T00:00:00Z", 500, 50.0, "unhealthy", "HTTP 500")
    db.save_check(a, "2026-01-01T00:05:00Z", 200, 40.0, "healthy")
    latest = {r["name"]: r for r in db.get_latest_checks()}
    assert latest["A"]["result"] == "healthy" and latest["A"]["response_time_ms"] == 40.0
    assert latest["B"]["result"] is None and latest["B"]["checked_at"] is None
    assert db.count_checks() == 2


def test_data_persists_across_instances(tmp_path):
    path = tmp_path / "persist.db"
    first = Database(path)
    eid = first.add_endpoint("API", "https://api.test")
    first.save_check(eid, "2026-01-01T00:00:00Z", 200, 12.0, "healthy")
    second = Database(path)
    assert len(second.list_endpoints()) == 1 and second.count_checks() == 1


def test_database_rejects_invalid_data(db):
    with pytest.raises(sqlite3.IntegrityError):  # unknown endpoint (foreign key)
        db.save_check(12345, "2026-01-01T00:00:00Z", 200, 1.0, "healthy")
    eid = db.add_endpoint("API", "https://api.test")
    with pytest.raises(sqlite3.IntegrityError):  # result not one of the allowed values
        db.save_check(eid, "2026-01-01T00:00:00Z", 200, 1.0, "great")
    assert db.count_checks() == 0


def test_sql_injection_text_is_stored_as_plain_data(db):
    nasty = "x'); DROP TABLE endpoints;--"
    db.add_endpoint(nasty, "https://safe.test")
    assert db.list_endpoints()[0]["name"] == nasty


def test_check_results_flow_into_database_and_report(db):
    eid = db.add_endpoint("API", "https://api.test")
    result = am.check_endpoint("https://api.test", http_get=fake_get(fake_response(404, "Not Found")))
    db.save_check(eid, result.checked_at, result.status_code, result.response_time_ms,
                  result.result, result.error_message)
    parsed = parse_csv(am.build_csv_report(db.get_history()))
    assert parsed[1][1] == "https://api.test"
    assert parsed[1][3] == "404" and parsed[1][5] == "Unhealthy"


# --------------------------------------------------------------------------- #
# Real requests against a local server (no external APIs involved)
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            time.sleep(1.5)
        status = {"/ok": 200, "/missing": 404, "/broken": 500, "/slow": 200}.get(self.path, 404)
        self.send_response(status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def local_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_real_request_healthy(local_server):
    result = am.check_endpoint(f"{local_server}/ok")
    assert (result.result, result.status_code) == (am.HEALTHY, 200)
    assert 0 < result.response_time_ms < 2000


def test_real_request_404_and_500(local_server):
    assert am.check_endpoint(f"{local_server}/missing").result == am.UNHEALTHY
    broken = am.check_endpoint(f"{local_server}/broken")
    assert (broken.result, broken.status_code) == (am.UNHEALTHY, 500)


def test_real_timeout(local_server):
    result = am.check_endpoint(f"{local_server}/slow", timeout=0.3)
    assert result.result == am.ERROR and "timed out" in result.error_message


def test_real_response_time_reflects_server_delay(local_server):
    result = am.check_endpoint(f"{local_server}/slow", timeout=5)
    assert result.result == am.HEALTHY and result.response_time_ms >= 1400


def test_real_connection_refused():
    result = am.check_endpoint("http://127.0.0.1:1/", timeout=2)
    assert result.result == am.ERROR and "Could not connect" in result.error_message


# --------------------------------------------------------------------------- #
# Dashboard flow (Streamlit test harness, real requests to the local server)
# --------------------------------------------------------------------------- #
def test_dashboard_add_check_and_remove_flow(local_server, tmp_path, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    db_path = tmp_path / "ui.db"
    monkeypatch.setenv("API_MONITOR_DB", str(db_path))
    st.cache_resource.clear()
    app_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")

    at = AppTest.from_file(app_file, default_timeout=60).run()
    assert not at.exception

    def add(name, url):
        next(t for t in at.sidebar.text_input if t.label == "Name (optional)").set_value(name)
        next(t for t in at.sidebar.text_input if t.label == "Endpoint URL").set_value(url)
        next(b for b in at.sidebar.button if b.label == "Add endpoint").click()
        at.run()

    add("Good", f"{local_server}/ok")
    add("Broken", f"{local_server}/broken")
    add("", "not a url")
    assert at.sidebar.error  # invalid URL is rejected with a message
    add("Same again", f"{local_server}/ok")
    assert at.sidebar.warning  # duplicate is rejected with a message

    next(b for b in at.button if b.label == "Run checks now").click()
    at.run()
    assert not at.exception
    stored = Database(db_path).get_latest_checks()
    assert {r["name"]: r["result"] for r in stored} == {"Broken": "unhealthy", "Good": "healthy"}

    at.sidebar.checkbox[0].check().run()
    next(b for b in at.sidebar.button if b.label == "Remove endpoint").click()
    at.run()
    assert not at.exception
    assert len(Database(db_path).list_endpoints()) == 1
    assert Database(db_path).count_checks() == 1

