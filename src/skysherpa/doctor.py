"""Health checks for every upstream dependency.

This is the most operationally important command in the project. A 24/7 box fails in
three ways that look identical from the outside: the network is down, VATSIM is down,
or VATSIM silently changed its schema. `doctor` separates them, and it is cheap enough
to run from cron every fifteen minutes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from . import net
from .config import ENDPOINTS, Settings
from .feed import FeedError, fetch_snapshot

__all__ = ["Check", "render", "run_checks"]

MAX_FEED_AGE_SECONDS = 300
MIN_AIRPORTS = 5_000
MIN_FIRS = 500
MIN_BOUNDARIES = 500


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    critical: bool = True
    skipped: bool = False
    elapsed_ms: int = 0


def _timed(name: str, fn, *, critical: bool = True) -> Check:
    started = time.monotonic()
    try:
        ok, detail = fn()
    except FeedError as exc:
        ok, detail = False, str(exc)
    except Exception as exc:  # noqa: BLE001 - a health check must never crash the caller
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return Check(
        name=name,
        ok=ok,
        detail=detail,
        critical=critical,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _feed(settings: Settings) -> tuple[bool, str]:
    snapshot = fetch_snapshot(settings)
    age = snapshot.age_seconds()
    if age is not None and age > MAX_FEED_AGE_SECONDS:
        return False, f"feed is stale: {age:.0f}s old"
    detail = (
        f"{snapshot.connected_clients:,} clients | {len(snapshot.pilots):,} pilots | "
        f"{len(snapshot.atc)} ATC | {len(snapshot.observers)} observers | "
        f"{len(snapshot.atis)} ATIS | age {age:.0f}s" if age is not None else "no timestamp"
    )
    if not snapshot.atc:
        return False, "feed parsed but contains no ATC positions"
    return True, detail


def _json_check(
    key: str,
    *,
    settings: Settings,
    validate,
    params: str = "",
    accept: str = "application/json",
) -> tuple[bool, str]:
    url = ENDPOINTS[key] + (f"?{params}" if params else "")
    payload, response = net.get_json(url, settings, retries=1, timeout=25, accept=accept)
    if payload is None:
        return False, f"{response.error} (status {response.status})"
    return validate(payload)


def _transceivers(payload) -> tuple[bool, str]:
    if not isinstance(payload, list) or not payload:
        return False, "empty or unexpected transceiver payload"
    first = payload[0] if isinstance(payload[0], dict) else {}
    if "callsign" not in first or "transceivers" not in first:
        return False, "transceiver schema changed (missing callsign/transceivers)"
    radios = sum(len(item.get("transceivers") or []) for item in payload if isinstance(item, dict))
    return True, f"{len(payload):,} clients | {radios:,} radio sites"


def _atc_online(payload) -> tuple[bool, str]:
    if not isinstance(payload, list):
        return False, "unexpected payload shape"
    return True, f"{len(payload):,} online sessions"


def _vatspy(response_text: str) -> tuple[bool, str]:
    if "[Airports]" not in response_text or "[FIRs]" not in response_text:
        return False, "VATSpy.dat is missing expected sections"
    count = sum(1 for line in response_text.splitlines() if line.count("|") >= 4)
    if count < MIN_AIRPORTS:
        return False, f"only {count:,} pipe-delimited records"
    return True, f"{len(response_text.splitlines()):,} lines | {count:,} records"


def _boundaries(payload) -> tuple[bool, str]:
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        return False, "not a GeoJSON FeatureCollection"
    features = payload.get("features") or []
    if len(features) < MIN_BOUNDARIES:
        return False, f"only {len(features):,} boundary features"
    return True, f"{len(features):,} polygons"


def _weather(payload) -> tuple[bool, str]:
    if not isinstance(payload, list) or not payload:
        return False, "empty weather payload"
    item = payload[0] if isinstance(payload[0], dict) else {}
    if not item.get("rawOb"):
        return False, "weather schema changed (missing rawOb)"
    return True, f"{len(payload)} station(s) | {item.get('fltCat', '?')} | {item.get('icaoId', '?')}"


def _taf(payload) -> tuple[bool, str]:
    if not isinstance(payload, list) or not payload:
        return False, "empty TAF payload"
    item = payload[0] if isinstance(payload[0], dict) else {}
    if "fcsts" not in item:
        return False, "TAF schema changed (missing fcsts)"
    return True, f"{len(payload)} station(s) | {len(item.get('fcsts') or [])} forecast groups"


def run_checks(settings: Settings, *, include_weather: bool = True) -> list[Check]:
    checks: list[Check] = []

    checks.append(_timed("feed", lambda: _feed(settings)))
    checks.append(
        _timed(
            "transceivers",
            lambda: _json_check("transceivers", settings=settings, validate=_transceivers),
        )
    )
    checks.append(
        _timed(
            "atc_online",
            lambda: _json_check("atc_online", settings=settings, validate=_atc_online),
        )
    )

    def _vatspy_check() -> tuple[bool, str]:
        response = net.get(ENDPOINTS["vatspy_dat"], settings, retries=1, timeout=25, accept="text/plain")
        if not response.ok:
            return False, f"{response.error} (status {response.status})"
        return _vatspy(response.text())

    checks.append(_timed("vatspy", _vatspy_check))
    checks.append(
        _timed(
            "boundaries",
            lambda: _json_check("boundaries", settings=settings, validate=_boundaries),
        )
    )

    if include_weather:
        checks.append(
            _timed(
                "metar",
                lambda: _json_check(
                    "metar", settings=settings, validate=_weather, params="ids=EGLL&format=json"
                ),
            )
        )
        checks.append(
            _timed(
                "taf",
                lambda: _json_check(
                    "taf", settings=settings, validate=_taf, params="ids=EGLL&format=json"
                ),
            )
        )

    # Documented as unreliable (ADR-0007): report, never fail the run.
    def _history() -> tuple[bool, str]:
        payload, response = net.get_json(
            f"{ENDPOINTS['atc_history']}?limit=1", settings, retries=1, timeout=8
        )
        if payload is None:
            return False, f"unavailable: {response.error} (documented, ADR-0007)"
        return True, "available"

    checks.append(_timed("atc_history", _history, critical=False))

    return checks


def render(checks: list[Check], *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    lines = [f"SkySherpa doctor - {now.strftime('%Y-%m-%dT%H:%M:%SZ')}", ""]
    width = max(len(c.name) for c in checks)
    for check in checks:
        status = "OK  " if check.ok else ("WARN" if not check.critical else "FAIL")
        lines.append(
            f"  {status}  {check.name.ljust(width)}  {check.detail}"
            f"  [{check.elapsed_ms} ms]"
        )
    failed = [c for c in checks if not c.ok and c.critical]
    warned = [c for c in checks if not c.ok and not c.critical]
    good = [c for c in checks if c.ok]
    lines.append("")
    summary = f"{len(good)}/{len(checks)} healthy"
    if warned:
        summary += f", {len(warned)} degraded (non-critical)"
    if failed:
        summary += f", {len(failed)} FAILED"
    lines.append(summary)
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(not c.ok and c.critical for c in checks) else 0
