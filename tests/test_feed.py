"""Feed parsing: the forgiving-but-loud layer between raw JSON and the models."""

from __future__ import annotations

import pytest

from skysherpa.feed import FeedError, parse_snapshot, parse_timestamp

UPDATED = "2026-11-09T21:49:41.2144888Z"  # 7 fractional digits, as the feed sends

PAYLOAD = {
    "general": {
        "version": 3,
        "reload": 1,
        "update": "20261109214941",
        "update_timestamp": UPDATED,
        "connected_clients": 1392,
        "unique_users": 1303,
    },
    "pilots": [
        {
            "cid": 1,
            "callsign": "BAW123",
            "latitude": 51.0,
            "longitude": -0.5,
            "altitude": "35000",
            "groundspeed": 450,
            "heading": 90,
            "transponder": "1234",
            "logon_time": "2026-11-09T19:00:00Z",
            "last_updated": "2026-11-09T21:49:40Z",
            "flight_plan": {
                "flight_rules": "I",
                "aircraft": "A20N/M-SDE2E3FGHIJ1RWXY/LB1",
                "aircraft_short": "A20N",
                "departure": "EGLL",
                "arrival": "LEPA",
                "alternate": "LEMH",
                "cruise_tas": "450",
                "altitude": "35000",
                "deptime": "1325",
                "enroute_time": "1008",
                "fuel_time": "1213",
                "route": "DCT GATBA DCT VATZI",
                "remarks": "RMK/TCAS SIMBRIEF",
            },
        },
        {
            "cid": 2,
            "callsign": "GABCD",
            "latitude": 51.5,
            "longitude": -0.1,
            "altitude": 0,
            "groundspeed": 0,
            "heading": 0,
            "transponder": "7000",
            "logon_time": "2026-11-09T21:00:00Z",
            "last_updated": "2026-11-09T21:49:40Z",
        },
    ],
    "controllers": [
        {
            "cid": 10,
            "callsign": "EGLL_TWR",
            "frequency": "118.500",
            "facility": 4,
            "rating": 4,
            "server": "UK",
            "visual_range": 40,
            "logon_time": "2026-11-09T18:00:00Z",
        },
        {
            "cid": 11,
            "callsign": "BAW15",
            "frequency": "199.998",  # unset sentinel
            "facility": 0,
            "rating": 1,
            "server": "UK",
            "visual_range": 0,
            "logon_time": "2026-11-09T20:00:00Z",
        },
    ],
    "atis": [{"callsign": "EGLL_ATIS", "frequency": "128.075", "atis_code": "A"}],
    "servers": [{"ident": "UK", "hostname": "uk.example"}],
    "prefiles": [{"callsign": "EZY1"}, {"callsign": "EZY2"}],
}


def test_timestamp_with_seven_fractional_digits() -> None:
    parsed = parse_timestamp(UPDATED)
    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.microsecond == 214488
    assert parsed.utcoffset() is not None


@pytest.mark.parametrize("value", ["", None, "not-a-date", "2026-13-45T99:99:99Z"])
def test_bad_timestamps_are_none(value: str | None) -> None:
    assert parse_timestamp(value) is None


def test_snapshot_counts() -> None:
    snapshot = parse_snapshot(PAYLOAD)
    assert snapshot.connected_clients == 1392
    assert snapshot.unique_users == 1303
    assert len(snapshot.pilots) == 2
    assert snapshot.prefiled == 2
    assert snapshot.servers == ["UK"]


def test_observers_are_separated_from_atc() -> None:
    snapshot = parse_snapshot(PAYLOAD)
    assert [c.callsign for c in snapshot.atc] == ["EGLL_TWR"]
    assert [c.callsign for c in snapshot.observers] == ["BAW15"]


def test_unset_frequency_becomes_none() -> None:
    snapshot = parse_snapshot(PAYLOAD)
    observer = snapshot.observers[0]
    assert observer.frequency_mhz is None


def test_pilot_flight_plan_parsed() -> None:
    snapshot = parse_snapshot(PAYLOAD)
    pilot = snapshot.pilots[0]
    plan = pilot.flight_plan
    assert plan is not None
    assert (plan.departure, plan.arrival) == ("EGLL", "LEPA")
    assert plan.aircraft_short == "A20N"
    assert plan.cruise_altitude_ft == 35000
    assert plan.enroute_minutes == 608  # "1008" is 10h08m, not 08:10
    assert plan.fuel_minutes == 733
    assert plan.is_complete


def test_pilot_without_plan_and_on_ground() -> None:
    snapshot = parse_snapshot(PAYLOAD)
    pilot = snapshot.pilots[1]
    assert pilot.flight_plan is None
    assert not pilot.airborne
    assert pilot.has_position


def test_airborne_detection() -> None:
    snapshot = parse_snapshot(PAYLOAD)
    assert snapshot.pilots[0].airborne
    assert len(snapshot.airborne) == 1


def test_pilots_with_plan() -> None:
    snapshot = parse_snapshot(PAYLOAD)
    assert len(snapshot.pilots_with_plan) == 1


def test_atis_airport_extracted() -> None:
    snapshot = parse_snapshot(PAYLOAD)
    assert snapshot.atis[0].airport == "EGLL"
    assert snapshot.atis[0].atis_code == "A"


def test_age_is_positive_for_past_timestamp() -> None:
    snapshot = parse_snapshot(PAYLOAD)
    age = snapshot.age_seconds()
    assert age is not None and age > 0


def test_feed_errors_are_explicit() -> None:
    with pytest.raises(FeedError):
        parse_snapshot([])  # type: ignore[arg-type]
    with pytest.raises(FeedError):
        parse_snapshot({"pilots": []})


def test_empty_sections_are_tolerated() -> None:
    payload = {"general": {"update_timestamp": UPDATED}}
    snapshot = parse_snapshot(payload)
    assert snapshot.pilots == []
    assert snapshot.controllers == []
    assert snapshot.atc == []
