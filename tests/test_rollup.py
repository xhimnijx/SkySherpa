"""Rollup and simulator tests.

The important property is absence: the presence matrix must contain offline slots, or the
forecaster learns that every position is always online. That bug shipped once already.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from skysherpa.rollup import rebuild_all, rebuild_slots
from skysherpa.simulate import simulate_flights, simulate_network
from skysherpa.store import Database


@pytest.fixture()
def simulated(tmp_path: Path):
    database = Database(tmp_path / "sim.sqlite3")
    database.initialize()
    end = datetime(2026, 11, 9, 22, tzinfo=UTC)
    network = simulate_network(database, days=21, seed=5, end=end, dropout_after_days=12)
    flights = simulate_flights(database, days=21, seed=6, end=end)
    result = rebuild_all(database)
    yield database, network, flights, result
    database.close()


def test_simulation_builds_a_network(simulated) -> None:
    database, network, flights, _ = simulated
    assert network.positions > 0
    assert network.sessions > 0
    assert flights > 0
    assert database.scalar("SELECT COUNT(*) FROM snapshot_stat") > 0
    assert database.scalar("SELECT COUNT(*) FROM controller_session") == network.sessions


def test_rollup_records_online_and_offline(simulated) -> None:
    database, _, _, result = simulated
    online = database.scalar("SELECT COUNT(*) FROM position_slot WHERE online = 1")
    offline = database.scalar("SELECT COUNT(*) FROM position_slot WHERE online = 0")
    assert result.slots > 0
    assert online > 0
    assert offline > online, "most observed slots should be absence, not presence"


def test_rollup_covers_every_position_and_day(simulated) -> None:
    database, network, _, _ = simulated
    positions = database.scalar("SELECT COUNT(DISTINCT position_key) FROM position_slot")
    days = database.scalar("SELECT COUNT(DISTINCT day) FROM position_slot")
    assert positions == network.positions
    assert days == 21


def test_rollup_builds_routes_and_airports(simulated) -> None:
    _, _, _, result = simulated
    assert result.pairs > 0
    assert result.airports > 0
    assert result.variants > 0


def test_rollup_is_idempotent(simulated) -> None:
    database, _, _, first = simulated
    second = rebuild_all(database)
    assert second.slots == first.slots
    assert second.pairs == first.pairs
    assert database.scalar("SELECT COUNT(*) FROM position_slot") == first.slots


def test_no_snapshots_means_no_presence_matrix(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.sqlite3")
    database.initialize()
    assert rebuild_slots(database) == 0
    database.close()


def test_dropout_position_stops_appearing(simulated) -> None:
    """LON_S_CTR is retired partway through; later days must show absence, not silence."""
    database, _, _, _ = simulated
    rows = database.query(
        "SELECT day, MAX(online) AS online FROM position_slot "
        "WHERE position_key = 'LON_S_CTR' GROUP BY day ORDER BY day"
    )
    assert rows
    assert sum(row["online"] or 0 for row in rows) > 0
    # The final week is past the dropout: the position is observed but absent.
    last_week = rows[-7:]
    assert all((row["online"] or 0) == 0 for row in last_week)


def test_simulated_flights_have_block_times(simulated) -> None:
    database, _, flights, _ = simulated
    recorded = database.scalar("SELECT COUNT(*) FROM flight WHERE block_min IS NOT NULL")
    assert recorded == flights
    assert database.scalar("SELECT AVG(block_min) FROM flight") > 0
