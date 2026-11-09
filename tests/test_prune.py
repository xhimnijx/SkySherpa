"""Retention: pruning must free the bulk without costing the forecast.

The safety property is the interesting one. Flights are pruned for their age; controller
sessions and the materialised presence matrix are not touched, because the forecaster reads
those and a lost session is a lost observation. If a prune ever removes a session or a
slot, the model quietly forgets history and nothing raises.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skysherpa.prune import prune_history
from skysherpa.rollup import rebuild_all
from skysherpa.simulate import simulate_flights, simulate_network
from skysherpa.store import Database


@pytest.fixture()
def history(tmp_path: Path):
    database = Database(tmp_path / "prune.sqlite3")
    database.initialize()
    end = datetime(2026, 11, 9, 22, tzinfo=UTC)
    simulate_network(database, days=30, seed=5, end=end)
    simulate_flights(database, days=30, seed=6, end=end)
    rebuild_all(database)
    yield database, end
    database.close()


def test_prune_removes_only_old_flights(history) -> None:
    database, end = history
    before = int(database.scalar("SELECT COUNT(*) FROM flight") or 0)
    recent = int(
        database.scalar(
            "SELECT COUNT(*) FROM flight WHERE last_seen >= ?",
            ((end - timedelta(days=14)).isoformat(),),
        )
        or 0
    )
    sessions_before = int(database.scalar("SELECT COUNT(*) FROM controller_session") or 0)
    slots_before = int(database.scalar("SELECT COUNT(*) FROM position_slot") or 0)

    result = prune_history(database, keep_days=14, now=end)

    assert result.flights > 0
    remaining = int(database.scalar("SELECT COUNT(*) FROM flight") or 0)
    assert remaining == recent
    assert remaining < before
    # The forecaster's raw material and its aggregate must be untouched.
    assert int(database.scalar("SELECT COUNT(*) FROM controller_session") or 0) == sessions_before
    assert int(database.scalar("SELECT COUNT(*) FROM position_slot") or 0) == slots_before


def test_prune_removes_the_orphaned_track_points(history) -> None:
    database, end = history
    prune_history(database, keep_days=14, now=end)
    orphans = int(
        database.scalar(
            "SELECT COUNT(*) FROM flight_track WHERE flight_id NOT IN (SELECT flight_id FROM flight)"
        )
        or 0
    )
    assert orphans == 0


def test_dry_run_changes_nothing(history) -> None:
    database, end = history
    before = int(database.scalar("SELECT COUNT(*) FROM flight") or 0)
    result = prune_history(database, keep_days=14, now=end, dry_run=True)
    assert result.flights > 0
    assert int(database.scalar("SELECT COUNT(*) FROM flight") or 0) == before


def test_prune_keeps_everything_inside_the_window(history) -> None:
    database, end = history
    before = int(database.scalar("SELECT COUNT(*) FROM flight") or 0)
    result = prune_history(database, keep_days=3650, now=end)
    assert result.flights == 0
    assert int(database.scalar("SELECT COUNT(*) FROM flight") or 0) == before


def test_keep_days_must_be_positive(history) -> None:
    database, end = history
    with pytest.raises(ValueError):
        prune_history(database, keep_days=0, now=end)
