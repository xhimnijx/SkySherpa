"""`fir_at` against real airspace geometry.

Every en-route number in the product depends on knowing which FIR a point is in: the export
maps route samples to FIRs, and the coverage model is keyed on them. The fixture holds the
real polygons for the 40 FIRs that own our captured airports, simplified to ~5 km, so the
join can be checked offline against the VATSpy airport->FIR mapping.

Measured on the capture: geometry agrees with VATSpy for 40 of 42 airports. The two misses
are the Sao Paulo pair, whose FIR appears under a different boundary code -- a mapping
quirk, not a geometric error, and not worth a special case.
"""

from __future__ import annotations

import json
from pathlib import Path

from skysherpa.geo import BoundingBox
from skysherpa.staticdata import Airport, Boundary, StaticReference

DATA = Path(__file__).resolve().parent / "data" / "geometry_sample.json"


def _reference() -> tuple[StaticReference, dict]:
    fixture = json.loads(DATA.read_text(encoding="utf-8"))
    airports = {
        code: Airport(code, data["name"], data["lat"], data["lon"], data["iata"], data["fir"])
        for code, data in fixture["airports"].items()
    }
    boundaries = [
        Boundary(
            code=entry["code"],
            bbox=BoundingBox(*entry["bbox"]),
            geometry={
                "type": "Polygon",
                "coordinates": [[(x, y) for x, y in ring] for ring in entry["rings"]],
            },
        )
        for entry in fixture["boundaries"]
    ]
    return StaticReference(airports=airports, firs={}, boundaries=boundaries), fixture["airports"]


def test_the_fixture_stays_small_enough_to_commit() -> None:
    assert DATA.stat().st_size < 200_000, "capture overshot; simplify more before committing"


def test_geometry_agrees_with_the_vatspy_mapping() -> None:
    reference, airports = _reference()
    agree = checked = 0
    for data in airports.values():
        if not data["fir"]:
            continue
        checked += 1
        if reference.fir_at(data["lat"], data["lon"]) == data["fir"]:
            agree += 1
    assert checked >= 30
    assert agree / checked >= 0.90, f"{agree}/{checked} airports resolved to their VATSpy FIR"


def test_major_airports_resolve_to_their_fir() -> None:
    reference, _ = _reference()
    expected = {
        "EGLL": "EGTT",
        "LFPG": "LFFF",
        "EDDF": "EDGG",
        "LEMD": "LECM",
        "KJFK": "KZNY",
        "KLAX": "KZLA",
    }
    checked = 0
    for code, fir in expected.items():
        airport = reference.airports.get(code)
        if airport is None:
            continue  # the capture only holds the airports the feed sample used
        assert reference.fir_at(airport.lat, airport.lon) == fir, code
        checked += 1
    assert checked >= 4, f"only {checked} of the expected airports were in the fixture"


def test_the_open_ocean_belongs_to_nobody() -> None:
    reference, _ = _reference()
    assert reference.fir_at(0.0, -30.0) is None
    assert reference.fir_at(-60.0, 0.0) is None


def test_fir_at_is_total_over_a_grid() -> None:
    """Whatever the input, the lookup must answer rather than raise."""
    reference, _ = _reference()
    for lat in range(-60, 71, 10):
        for lon in range(-180, 181, 20):
            result = reference.fir_at(float(lat), float(lon))
            assert result is None or isinstance(result, str)
