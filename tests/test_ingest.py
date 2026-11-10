"""Ingest state machine tests.

Every case here is something that happens on a real network: a blip, a retry, a
controller who reconnects, a gap in our own polling, an aircraft that lands.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from factories import controller, flight_plan, pilot, snapshot, stamp, when

from skysherpa.identity import Identity
from skysherpa.ingest import IngestConfig, Ingestor
from skysherpa.store import Database, to_iso


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "ingest.sqlite3")
    database.initialize()
    yield database
    database.close()


@pytest.fixture()
def ingestor(db: Database) -> Ingestor:
    return Ingestor(db, identity=Identity.from_secret("test"), config=IngestConfig(interval_s=30))


def sessions(db: Database):
    return db.query("SELECT * FROM controller_session ORDER BY started_at")


def poll(ingestor: Ingestor, hour: int, minute: int, second: int = 0, **kwargs):
    return ingestor.poll(snapshot(stamp(hour, minute, second), **kwargs))


#: A realistic snapshot is never completely empty: even when the controllers we care
#: about have gone, other aircraft are still flying. Tests that assert absence need one.
FILLER = [pilot("FILL1", altitude=0, groundspeed=0)]


# ------------------------------------------------------------------ sessions


def test_first_poll_opens_session(db: Database, ingestor: Ingestor) -> None:
    result = poll(ingestor, 22, 0, controllers=[controller("EGLL_TWR")])
    assert result.accepted
    assert result.sessions_opened == 1
    rows = sessions(db)
    assert len(rows) == 1
    assert rows[0]["callsign"] == "EGLL_TWR"
    assert rows[0]["ended_at"] == to_iso(when(22, 0))
    assert rows[0]["duration_s"] == 0
    assert rows[0]["n_samples"] == 1


def test_session_updates_every_poll(db: Database, ingestor: Ingestor) -> None:
    poll(ingestor, 22, 0, controllers=[controller("EGLL_TWR")])
    poll(ingestor, 22, 0, 30, controllers=[controller("EGLL_TWR")])
    row = sessions(db)[0]
    assert row["ended_at"] == to_iso(when(22, 0, 30))
    assert row["duration_s"] == 30
    assert row["n_samples"] == 2


def test_session_ends_at_last_sighting(db: Database, ingestor: Ingestor) -> None:
    poll(ingestor, 22, 0, controllers=[controller("EGLL_TWR")])
    poll(ingestor, 22, 0, 30, controllers=[controller("EGLL_TWR")])
    result = poll(ingestor, 22, 1, pilots=FILLER)
    assert result.sessions_closed == 1
    row = sessions(db)[0]
    # We only know they were offline somewhere between 22:00:30 and 22:01:00.
    assert row["ended_at"] == to_iso(when(22, 0, 30))
    assert row["duration_s"] == 30


def test_observer_is_not_recorded(db: Database, ingestor: Ingestor) -> None:
    result = poll(
        ingestor, 22, 0, controllers=[controller("BAW15", facility=0, frequency="199.998")]
    )
    assert result.sessions_opened == 0
    assert sessions(db) == []
    assert db.scalar("SELECT COUNT(*) FROM position") == 0


def test_atis_is_not_recorded_as_atc(db: Database, ingestor: Ingestor) -> None:
    result = poll(ingestor, 22, 0, controllers=[controller("EGLL_ATIS", facility=4)])
    assert result.sessions_opened == 0
    assert sessions(db) == []


def test_duplicate_snapshot_is_ignored(db: Database, ingestor: Ingestor) -> None:
    poll(ingestor, 22, 0, controllers=[controller("EGLL_TWR")])
    result = poll(ingestor, 22, 0, controllers=[controller("EGLL_TWR")])
    assert result.duplicate
    assert db.scalar("SELECT COUNT(*) FROM snapshot_stat") == 1
    assert sessions(db)[0]["n_samples"] == 1


def test_reconnect_inside_window_merges_sessions(db: Database, ingestor: Ingestor) -> None:
    poll(ingestor, 22, 0, controllers=[controller("EGLL_TWR")])
    poll(ingestor, 22, 0, 30, controllers=[controller("EGLL_TWR")])
    poll(ingestor, 22, 1, pilots=FILLER)
    poll(ingestor, 22, 1, 30, controllers=[controller("EGLL_TWR")])
    rows = sessions(db)
    assert len(rows) == 1, "a 60-second dropout must not create a second shift"
    assert rows[0]["ended_at"] == to_iso(when(22, 1, 30))
    assert rows[0]["n_samples"] == 3


def test_reconnect_after_window_opens_new_session(db: Database, ingestor: Ingestor) -> None:
    poll(ingestor, 22, 0, controllers=[controller("EGLL_TWR")])
    poll(ingestor, 22, 10, pilots=FILLER)
    poll(ingestor, 22, 20, controllers=[controller("EGLL_TWR")])
    rows = sessions(db)
    assert len(rows) == 2


def test_frequency_change_keeps_one_session(db: Database, ingestor: Ingestor) -> None:
    poll(ingestor, 22, 0, controllers=[controller("EGLL_TWR", frequency="118.500")])
    poll(ingestor, 22, 0, 30, controllers=[controller("EGLL_TWR", frequency="118.700")])
    assert len(sessions(db)) == 1


def test_gap_is_recorded(db: Database, ingestor: Ingestor) -> None:
    poll(ingestor, 22, 0, controllers=[controller("EGLL_TWR")])
    result = poll(ingestor, 22, 5, controllers=[controller("EGLL_TWR")])
    assert result.gap_seconds == 300
    gap = db.query("SELECT * FROM ingest_gap")[0]
    assert gap["started_at"] == to_iso(when(22, 0))
    assert gap["ended_at"] == to_iso(when(22, 5))


def test_no_gap_for_normal_cadence(db: Database, ingestor: Ingestor) -> None:
    poll(ingestor, 22, 0, controllers=[controller("EGLL_TWR")])
    poll(ingestor, 22, 0, 30, controllers=[controller("EGLL_TWR")])
    assert db.scalar("SELECT COUNT(*) FROM ingest_gap") == 0


def test_position_row_carries_airport_candidate(db: Database, ingestor: Ingestor) -> None:
    poll(ingestor, 22, 0, controllers=[controller("EGLL_TWR")])
    row = db.query("SELECT * FROM position")[0]
    assert row["position_key"] == "EGLL_TWR"
    assert row["facility"] == "TWR"
    assert row["airport_icao"] == "EGLL"


def test_empty_feed_is_rejected(db: Database, ingestor: Ingestor) -> None:
    result = ingestor.poll(snapshot(stamp(22, 0)))
    assert not result.accepted
    assert result.rejected_reason == "empty_feed"
    assert db.scalar("SELECT accepted FROM snapshot_stat") == 0


# ------------------------------------------------------------------- flights


def test_flight_lifecycle(db: Database, ingestor: Ingestor) -> None:
    poll(ingestor, 22, 0, pilots=[pilot("BAW1", plan=flight_plan())])
    poll(ingestor, 22, 1, pilots=[pilot("BAW1", plan=flight_plan(), lat=51.2, lon=-0.2)])
    poll(ingestor, 22, 2, pilots=[pilot("BAW1", plan=flight_plan(), lat=51.4, lon=0.1)])
    result = poll(ingestor, 22, 10, pilots=FILLER)  # beyond the 90 s grace
    assert result.flights_closed == 1

    row = db.query("SELECT * FROM flight")[0]
    assert row["dep_icao"] == "EGLL"
    assert row["arr_icao"] == "LEPA"
    assert row["off_block"] == to_iso(when(22, 0))
    assert row["on_block"] == to_iso(when(22, 2))
    assert row["block_min"] == 2
    assert row["n_track_points"] >= 2


def test_flight_track_is_simplified(db: Database, ingestor: Ingestor) -> None:
    for index in range(12):
        poll(
            ingestor,
            22,
            index,
            pilots=[
                pilot(
                    "BAW1",
                    plan=flight_plan(),
                    lat=50.0 + index * 0.1,
                    lon=-1.0 + index * 0.2,
                )
            ],
        )
    poll(ingestor, 22, 15, pilots=FILLER)  # beyond the 90 s grace
    row = db.query("SELECT n_track_points FROM flight")[0]
    stored = db.scalar("SELECT COUNT(*) FROM flight_track")
    assert row["n_track_points"] == stored
    assert 2 <= stored <= 4, "a straight track must collapse to its endpoints"


def test_flight_opens_on_first_plan(db: Database, ingestor: Ingestor) -> None:
    result = poll(ingestor, 22, 0, pilots=[pilot("BAW1", plan=flight_plan())])
    assert result.flights_opened == 1
    assert db.scalar("SELECT COUNT(*) FROM flight") == 1


def test_pilot_without_plan_is_ignored(db: Database, ingestor: Ingestor) -> None:
    result = poll(ingestor, 22, 0, pilots=[pilot("GABCD", altitude=0, groundspeed=0)])
    assert result.flights_opened == 0
    assert db.scalar("SELECT COUNT(*) FROM flight") == 0


def test_ground_pilot_still_records_flight_but_no_block_time(
    db: Database, ingestor: Ingestor
) -> None:
    poll(ingestor, 22, 0, pilots=[pilot("BAW1", plan=flight_plan(), altitude=0, groundspeed=0)])
    poll(ingestor, 22, 1)
    row = db.query("SELECT * FROM flight")[0]
    assert row["off_block"] is None
    assert row["block_min"] is None


def test_route_hash_is_recorded(db: Database, ingestor: Ingestor) -> None:
    poll(ingestor, 22, 0, pilots=[pilot("BAW1", plan=flight_plan(route="DCT GATBA DCT VATZI"))])
    row = db.query("SELECT route_hash FROM flight")[0]
    assert row["route_hash"] and len(row["route_hash"]) == 16


def test_finalize_flushes_open_flight(db: Database, ingestor: Ingestor) -> None:
    poll(ingestor, 22, 0, pilots=[pilot("BAW1", plan=flight_plan())])
    ingestor.finalize()
    row = db.query("SELECT on_block FROM flight")[0]
    assert row["on_block"] is not None


def test_snapshot_stat_records_counts(db: Database, ingestor: Ingestor) -> None:
    poll(
        ingestor,
        22,
        0,
        controllers=[controller("EGLL_TWR"), controller("BAW15", facility=0)],
        pilots=[pilot("BAW1", plan=flight_plan())],
    )
    row = db.query("SELECT * FROM snapshot_stat")[0]
    assert row["pilots"] == 1
    assert row["controllers"] == 2
    assert row["atc_positions"] == 1
    assert row["observers"] == 1


# ---------------------------------------------------------------- hysteresis


def test_single_missed_poll_does_not_split_a_flight(db: Database, ingestor: Ingestor) -> None:
    """One absent snapshot is an artefact, not a landing.

    On live data this produced two rows for one flight and left 48% of the table with no
    block time -- phantom flights that also inflated the route popularity counts.
    """
    crew = pilot("BAW123", plan=flight_plan("EGLL", "LEPA"))
    poll(ingestor, 22, 0, pilots=[crew])
    poll(ingestor, 22, 1, pilots=FILLER)
    poll(ingestor, 22, 1, 30, pilots=[crew])
    assert db.scalar("SELECT COUNT(*) FROM flight") == 1


def test_long_absence_closes_the_flight(db: Database, ingestor: Ingestor) -> None:
    crew = pilot("BAW123", plan=flight_plan("EGLL", "LEPA"))
    poll(ingestor, 22, 0, pilots=[crew])
    poll(ingestor, 22, 10, pilots=FILLER)
    poll(ingestor, 22, 11, pilots=[crew])
    assert db.scalar("SELECT COUNT(*) FROM flight") == 2


def test_missing_plan_does_not_close_the_flight(db: Database, ingestor: Ingestor) -> None:
    crew = pilot("BAW123", plan=flight_plan("EGLL", "LEPA"))
    poll(ingestor, 22, 0, pilots=[crew])
    poll(ingestor, 22, 1, pilots=[pilot("BAW123")])  # present, but the plan vanished
    poll(ingestor, 22, 2, pilots=[crew])
    assert db.scalar("SELECT COUNT(*) FROM flight") == 1
