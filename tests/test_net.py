"""HTTP client behaviour: what happens when VATSIM's edge says no.

The feed sits behind Cloudflare and answers a burst with 403. That is a throttle, not a ban
-- the same client succeeds seconds later -- so 403 must be retried with backoff, while a
404 must not be retried at all. Getting that wrong either stops a 24/7 collector or hammers
an edge that is asking it to slow down.
"""

from __future__ import annotations

import gzip
import io
import json
import urllib.error

import pytest

from skysherpa import net
from skysherpa.config import Settings


class FakeResponse:
    def __init__(self, body: bytes = b"{}", status: int = 200, headers: dict | None = None):
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture()
def settings() -> Settings:
    return Settings.load()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Backoff is a real sleep; the tests must not pay it."""
    monkeypatch.setattr(net.time, "sleep", lambda seconds: None)


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.test/feed", code, f"HTTP {code}", headers or {}, io.BytesIO(b"denied")
    )


def test_a_403_is_retried_and_then_succeeds(monkeypatch, settings) -> None:
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(403)
        return FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(urllib.error, "HTTPError", urllib.error.HTTPError)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    response = net.get("https://example.test/feed", settings, retries=3)
    assert response.ok is True
    assert response.attempts == 2
    assert calls["n"] == 2


def test_a_404_is_not_retried(monkeypatch, settings) -> None:
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise _http_error(404)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    response = net.get("https://example.test/feed", settings, retries=5)
    assert response.ok is False
    assert response.status == 404
    assert response.attempts == 1
    assert calls["n"] == 1, "a 404 is a real answer, not a throttle"


def test_a_connection_error_is_retried_until_the_budget_runs_out(monkeypatch, settings) -> None:
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    response = net.get("https://example.test/feed", settings, retries=3)
    assert response.ok is False
    assert response.status == 0
    assert response.attempts == 3
    assert calls["n"] == 3
    assert "URLError" in (response.error or "")


def test_gzip_bodies_are_decompressed(monkeypatch, settings) -> None:
    payload = json.dumps({"pilots": 3}).encode()
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: FakeResponse(gzip.compress(payload), headers={}),
    )
    response = net.get("https://example.test/feed", settings)
    assert response.ok is True
    assert response.json() == {"pilots": 3}


def test_retry_after_is_honoured() -> None:
    assert net._retry_delay(0, "2") == 2.0
    assert net._retry_delay(3, "0") == 0.0
    # A junk header falls back to exponential backoff with jitter.
    delay = net._retry_delay(1, "later")
    assert 0.75 * 2 * 0.7 <= delay <= 0.75 * 2 * 1.3


def test_retry_delay_is_capped() -> None:
    assert net._retry_delay(10, "9999") == 30.0
    assert net._retry_delay(10, None) <= 30.0 * 1.3


def test_invalid_json_is_reported_not_raised(monkeypatch, settings) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: FakeResponse(b"<html>not json</html>"),
    )
    payload, response = net.get_json("https://example.test/feed", settings)
    assert payload is None
    assert response.ok is False
    assert "invalid JSON" in (response.error or "")


def test_json_success(monkeypatch, settings) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: FakeResponse(b'{"pilots": []}'),
    )
    payload, response = net.get_json("https://example.test/feed", settings)
    assert payload == {"pilots": []}
    assert response.ok is True


def test_response_helpers() -> None:
    response = net.Response(ok=True, status=200, body=b"hello")
    assert response.size == 5
    assert response.text() == "hello"
