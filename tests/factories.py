"""Snapshot factories for tests.

Built through the real :func:`skysherpa.feed.parse_snapshot` so the tests exercise the
same coercion path the ingest loop uses -- a factory that hands back dataclasses directly
would happily pass while the parser was broken.
"""

from __future__ import annotations

from datetime import UTC, datetime

from skysherpa.feed import Snapshot, parse_snapshot

__all__ = ["controller", "flight_plan", "pilot", "snapshot", "stamp"]

DAY = "2026-11-09"


def stamp(hour: int, minute: int = 0, second: int = 0, day: str = DAY) -> str:
    return f"{day}T{hour:02d}:{minute:02d}:{second:02d}Z"


def when(hour: int, minute: int = 0, second: int = 0, day: str = DAY) -> datetime:
    return datetime(2026, 11, 9, hour, minute, second, tzinfo=UTC) if day == DAY else (
        datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:{second:02d}+00:00")
    )


def controller(
    callsign: str,
    *,
    facility: int = 4,
    cid: int = 100,
    frequency: str = "118.500",
    rating: int = 4,
    server: str = "UK",
) -> dict:
    return {
        "cid": cid,
        "callsign": callsign,
        "frequency": frequency,
        "facility": facility,
        "rating": rating,
        "server": server,
        "visual_range": 40,
        "logon_time": "2026-11-09T18:00:00Z",
    }


def flight_plan(
    departure: str = "EGLL",
    arrival: str = "LEPA",
    *,
    route: str = "DCT GATBA DCT VATZI",
    aircraft: str = "A20N",
) -> dict:
    return {
        "flight_rules": "I",
        "aircraft": f"{aircraft}/M-SDE2E3",
        "aircraft_short": aircraft,
        "departure": departure,
        "arrival": arrival,
        "alternate": "LEMH",
        "cruise_tas": "450",
        "altitude": "35000",
        "deptime": "1200",
        "enroute_time": "0200",
        "fuel_time": "0245",
        "route": route,
        "remarks": "",
    }


def pilot(
    callsign: str,
    *,
    lat: float = 51.0,
    lon: float = -0.5,
    altitude: int = 30000,
    groundspeed: int = 400,
    heading: int = 90,
    cid: int = 200,
    plan: dict | None = None,
) -> dict:
    payload = {
        "cid": cid,
        "callsign": callsign,
        "latitude": lat,
        "longitude": lon,
        "altitude": str(altitude),
        "groundspeed": groundspeed,
        "heading": heading,
        "transponder": "1234",
        "logon_time": "2026-11-09T18:00:00Z",
        "last_updated": "2026-11-09T22:00:00Z",
    }
    if plan is not None:
        payload["flight_plan"] = plan
    return payload


def snapshot(ts: str, *, controllers=(), pilots=()) -> Snapshot:
    return parse_snapshot(
        {
            "general": {
                "version": 3,
                "reload": 1,
                "update": "20261109220000",
                "update_timestamp": ts,
                "connected_clients": len(pilots) + len(controllers),
                "unique_users": len(pilots),
            },
            "pilots": list(pilots),
            "controllers": list(controllers),
            "atis": [],
            "servers": [],
            "prefiles": [],
        }
    )
