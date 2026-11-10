"""Store tests: schema application, session bookkeeping, flight bookkeeping."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from factories import when

from skysherpa.store import Database, from_iso, to_iso


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    yield database
    database.close()


def test_initialize_sets_version(db: Database) -> None:
    assert db.version == 1


def test_schema_creates_expected_tables(db: Database) -> None:
    rows = db.query("SELECT name FROM sqlite_master WHERE type='table'")
    names = {row["name"] for row in rows}
    assert {
        "snapshot_stat",
        "ingest_gap",
        "position",
        "controller_session",
        "position_slot",
        "flight",
        "flight_track",
        "route_variant",
        "airport_hourly",
        "forecast",
        "plan_run",
        "user_fleet",
    } <= names


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "twice.sqlite3"
    for _ in range(2):
        database = Database(path)
        database.initialize()
        database.close()
    database = Database(path)
    database.initialize()
    assert database.version == 1
    database.close()


def test_wal_mode_enabled(db: Database) -> None:
    assert str(db.scalar("PRAGMA journal_mode")).lower() == "wal"


def test_record_snapshot_and_stats(db: Database) -> None:
    db.record_snapshot(
        {
            "ts": to_iso(when(22)),
            "feed_update": "20261109220000",
            "pilots": 1200,
            "controllers": 90,
            "atc_positions": 50,
            "observers": 40,
            "atis": 80,
            "prefiled": 100,
        }
    )
    stats = db.stats()
    assert stats["snapshots"] == 1
    assert stats["first_poll"] == when(22)
    assert stats["last_poll"] == when(22)


def test_duplicate_snapshot_replaces_not_duplicates(db: Database) -> None:
    row = {"ts": to_iso(when(22)), "pilots": 1}
    db.record_snapshot(row)
    db.record_snapshot(row)
    assert db.scalar("SELECT COUNT(*) FROM snapshot_stat") == 1


def test_gap_recorded(db: Database) -> None:
    db.record_gap(when(22), when(22) + timedelta(minutes=5), "poll_gap")
    row = db.query("SELECT * FROM ingest_gap")[0]
    assert row["reason"] == "poll_gap"
    assert from_iso(row["ended_at"]) == when(22) + timedelta(minutes=5)


def test_position_upsert_is_idempotent_and_keeps_first_resolution(db: Database) -> None:
    db.upsert_position("EGLL_TWR", "TWR", None, None, when(22), when(22))
    db.upsert_position("EGLL_TWR", "TWR", "EGLL", "EGTT", when(22), when(22) + timedelta(minutes=1))
    rows = db.query("SELECT * FROM position")
    assert len(rows) == 1
    assert rows[0]["airport_icao"] == "EGLL"
    assert rows[0]["fir_code"] == "EGTT"
    assert rows[0]["first_seen"] == to_iso(when(22))


def test_session_lifecycle(db: Database) -> None:
    db.upsert_position("EGLL_TWR", "TWR", "EGLL", "EGTT", when(22), when(22))
    db.start_session(
        session_key="abc",
        position_key="EGLL_TWR",
        pseudo_id="deadbeef",
        callsign="EGLL_TWR",
        facility_code=4,
        frequency_mhz=118.5,
        rating=4,
        server="UK",
        started_at=when(22),
    )
    db.update_session(
        "abc",
        ended_at=when(22) + timedelta(minutes=45),
        n_samples=90,
        started_at=when(22),
    )
    row = db.query("SELECT * FROM controller_session")[0]
    assert row["duration_s"] == 45 * 60
    assert row["n_samples"] == 90


def test_session_requires_known_position(db: Database) -> None:
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.start_session(
            session_key="orphan",
            position_key="NOPE_TWR",
            pseudo_id="x",
            callsign="NOPE_TWR",
            facility_code=4,
            frequency_mhz=None,
            rating=0,
            server="",
            started_at=when(22),
        )


def test_flight_lifecycle_and_block_time(db: Database) -> None:
    flight_id = db.start_flight(
        callsign="BAW1",
        pseudo_id="p",
        departure="EGLL",
        arrival="LEPA",
        alternate="LEMH",
        aircraft_short="A20N",
        aircraft_full="A20N/M-S",
        flight_rules="I",
        cruise_alt_ft=35000,
        cruise_tas_kt=450,
        filed_deptime="1200",
        filed_enroute_min=120,
        filed_route="DCT GATBA",
        route_hash="abc123",
        first_seen=when(22),
        max_gs_kt=400,
    )
    assert flight_id > 0
    db.finish_flight(
        flight_id,
        last_seen=when(22) + timedelta(minutes=118),
        off_block=when(22),
        on_block=when(22) + timedelta(minutes=118),
        n_track_points=12,
    )
    row = db.query("SELECT * FROM flight")[0]
    assert row["block_min"] == 118
    assert row["n_track_points"] == 12
    assert row["on_block"] is not None


def test_track_points_are_written(db: Database) -> None:
    flight_id = db.start_flight(
        callsign="BAW1",
        pseudo_id="p",
        departure="EGLL",
        arrival="LEPA",
        alternate="",
        aircraft_short="A20N",
        aircraft_full="",
        flight_rules="I",
        cruise_alt_ft=35000,
        cruise_tas_kt=450,
        filed_deptime="1200",
        filed_enroute_min=120,
        filed_route="",
        route_hash="",
        first_seen=when(22),
        max_gs_kt=400,
    )
    points = [
        (when(22), 51.0, -0.5, 30000, 400, 90),
        (when(22) + timedelta(minutes=1), 51.1, -0.3, 31000, 410, 90),
    ]
    db.append_track(flight_id, points)
    db.append_track(flight_id, points)  # idempotent rewrite, not a duplicate
    assert db.scalar("SELECT COUNT(*) FROM flight_track") == 2


def test_orphan_flights_are_closed(db: Database) -> None:
    flight_id = db.start_flight(
        callsign="BAW2",
        pseudo_id="p",
        departure="EGLL",
        arrival="LEPA",
        alternate="",
        aircraft_short="A20N",
        aircraft_full="",
        flight_rules="I",
        cruise_alt_ft=35000,
        cruise_tas_kt=450,
        filed_deptime="",
        filed_enroute_min=None,
        filed_route="",
        route_hash="",
        first_seen=when(22),
        max_gs_kt=400,
    )
    db.update_flight(flight_id, last_seen=when(23), max_gs_kt=420, off_block=when(22))
    closed = db.close_orphan_flights(before=when(23) + timedelta(minutes=1))
    assert closed == 1
    row = db.query("SELECT on_block FROM flight")[0]
    assert row["on_block"] == to_iso(when(23))


def test_iso_round_trip() -> None:
    moment = when(22, 31, 7)
    assert from_iso(to_iso(moment)) == moment
    assert from_iso(None) is None
    assert from_iso("not-a-date") is None
