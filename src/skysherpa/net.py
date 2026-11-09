"""HTTP client: stdlib only, gzip, jittered backoff, honest user agent.

VATSIM's edge is behind Cloudflare and will return 403 to a burst of requests. That
is a throttle, not a ban: the same client succeeds seconds later. So 403 is treated
as retryable *with backoff* -- never as a reason to poll harder.
"""

from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Settings

__all__ = ["Response", "get", "get_json"]

RETRYABLE = frozenset({403, 408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class Response:
    ok: bool
    status: int
    url: str = ""
    body: bytes = b""
    elapsed_ms: int = 0
    error: str | None = None
    attempts: int = 1

    @property
    def size(self) -> int:
        return len(self.body)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def _retry_delay(attempt: int, retry_after: str | None, *, base: float = 0.75, cap: float = 30.0) -> float:
    if retry_after:
        try:
            return min(cap, max(0.0, float(retry_after)))
        except ValueError:
            pass
    backoff = min(cap, base * (2 ** attempt))
    return backoff * (0.7 + random.random() * 0.6)  # full-ish jitter


def get(
    url: str,
    settings: Settings,
    *,
    retries: int | None = None,
    timeout: float | None = None,
    accept: str = "application/json",
) -> Response:
    """GET *url* with retries. Never raises for network conditions; inspect ``.ok``."""
    attempts = settings.retries if retries is None else retries
    attempts = max(1, attempts)
    timeout = settings.timeout_seconds if timeout is None else timeout

    last: Response | None = None
    for attempt in range(attempts):
        started = time.monotonic()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": accept,
                "Accept-Encoding": "gzip",
                "Connection": "close",
            },
        )
        delay = 0.0
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                raw = resp.read()
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
                if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        pass
                return Response(
                    ok=True,
                    status=getattr(resp, "status", 200),
                    url=url,
                    body=raw,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempt + 1,
                )
        except urllib.error.HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            try:
                body = exc.read()[:4000]
            except Exception:  # pragma: no cover - defensive
                body = b""
            last = Response(
                ok=False,
                status=exc.code,
                url=url,
                body=body,
                elapsed_ms=elapsed,
                error=f"HTTP {exc.code}",
                attempts=attempt + 1,
            )
            if exc.code not in RETRYABLE:
                return last
            delay = _retry_delay(attempt, exc.headers.get("Retry-After") if exc.headers else None)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            last = Response(
                ok=False,
                status=0,
                url=url,
                elapsed_ms=elapsed,
                error=f"{type(exc).__name__}: {exc}",
                attempts=attempt + 1,
            )
            delay = _retry_delay(attempt, None)

        if attempt < attempts - 1:
            time.sleep(delay)

    assert last is not None
    return last


def get_json(url: str, settings: Settings, **kwargs: Any) -> tuple[Any | None, Response]:
    """GET and decode JSON. Returns ``(payload, response)``; payload is None on failure."""
    response = get(url, settings, **kwargs)
    if not response.ok:
        return None, response
    try:
        return response.json(), response
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, Response(
            ok=False,
            status=response.status,
            url=url,
            body=response.body[:2000],
            elapsed_ms=response.elapsed_ms,
            error=f"invalid JSON: {exc}",
            attempts=response.attempts,
        )
