"""Live network checks. Deselect by default; run with `pytest -m network`.

These are the tests that would catch VATSIM changing its schema, so they are worth
the flakiness they occasionally cost.
"""

from __future__ import annotations

import pytest

from skysherpa.config import Settings
from skysherpa.doctor import run_checks
from skysherpa.feed import fetch_snapshot
from skysherpa.routes import parse_route

pytestmark = pytest.mark.network


def test_live_feed_parses() -> None:
    snapshot = fetch_snapshot(Settings.load())
    assert snapshot.pilots, "feed returned no pilots"
    assert snapshot.atc, "feed returned no ATC positions"
    assert len(snapshot.atc) < len(snapshot.controllers), "expected observers in controllers[]"
    assert snapshot.connected_clients > 0
    assert snapshot.update_raw


def test_live_routes_tokenise() -> None:
    snapshot = fetch_snapshot(Settings.load())
    plans = [p.flight_plan for p in snapshot.pilots if p.flight_plan and p.flight_plan.has_route]
    assert plans
    parsed = [parse_route(plan.route) for plan in plans[:200] if plan]
    known = sum(1 for route in parsed if route.n_fixes + route.n_airways > 0)
    # If this drops, the tokeniser has drifted from the corpus.
    assert known / len(parsed) > 0.9


def test_critical_sources_healthy() -> None:
    checks = run_checks(Settings.load(), include_weather=True)
    failed = [c for c in checks if c.critical and not c.ok]
    assert not failed, [(c.name, c.detail) for c in failed]
