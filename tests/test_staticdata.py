"""Static reference parsing: VATSpy sections and boundary polygons."""

from __future__ import annotations

from skysherpa.staticdata import StaticReference

VATSPY = """[Countries]
United Kingdom|EG|
Germany|ED|
[Airports]
EGLL|London Heathrow|51.4775|-0.461389|LHR|EGTT|0
EGSS|London Stansted|51.885|0.235|STN|EGTT|0
EDDF|Frankfurt Main|50.0333|8.5706|FRA|EDGG|0
[FIRs]
EGTT|London|LON|EGTT
EGTT-C|London ACC (Central) - London|LON_C|EGTT-C
EDGG|Langen|LANGEN|EDGG
[UIRs]
EGPX|Scottish|SCO|EGPX
[IDL]
ignore me
"""

BOUNDARIES = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"id": "EGTT", "label_lat": 51.5, "label_lon": -1.0},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0.0, 50.0], [2.0, 50.0], [2.0, 52.0], [0.0, 52.0], [0.0, 50.0]]],
            },
        },
        {
            "type": "Feature",
            "properties": {"id": "EGPX", "label_lat": 55.0, "label_lon": -3.0},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[-5.0, 45.0], [5.0, 45.0], [5.0, 55.0], [-5.0, 55.0], [-5.0, 45.0]]
                ],
            },
        },
        {
            "type": "Feature",
            "properties": {"id": "EDGG"},
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [
                    [[[8.0, 49.0], [9.0, 49.0], [9.0, 51.0], [8.0, 51.0], [8.0, 49.0]]]
                ],
            },
        },
    ],
}


def test_parse_vatspy_airports() -> None:
    reference = StaticReference.parse_vatspy(VATSPY)
    assert set(reference.airports) == {"EGLL", "EGSS", "EDDF"}
    heathrow = reference.airports["EGLL"]
    assert heathrow.name == "London Heathrow"
    assert heathrow.lat == 51.4775
    assert heathrow.fir == "EGTT"
    assert heathrow.iata == "LHR"


def test_parse_vatspy_firs() -> None:
    reference = StaticReference.parse_vatspy(VATSPY)
    assert "EGTT" in reference.firs
    assert reference.firs["EGTT"].prefix == "LON"
    assert "EGPX" in reference.firs  # UIRs are retained
    assert "IDL" not in str(reference.firs)


def test_malformed_lines_are_skipped() -> None:
    reference = StaticReference.parse_vatspy(
        "[Airports]\nbad line\nEGLL|Heathrow|not-a-number|0|LHR|EGTT|0\n"
        "EGSS|Stansted|51.885|0.235|STN|EGTT|0\n"
    )
    assert set(reference.airports) == {"EGSS"}


def test_resolve_fir_from_airport() -> None:
    reference = StaticReference.parse_vatspy(VATSPY)
    assert reference.resolve_fir("EGLL") == "EGTT"
    assert reference.resolve_fir("EGLL_TWR") == "EGTT"


def test_resolve_fir_from_fir_code_and_prefix() -> None:
    reference = StaticReference.parse_vatspy(VATSPY)
    assert reference.resolve_fir("EGTT") == "EGTT"
    assert reference.resolve_fir("LON_S") == "EGTT"
    assert reference.resolve_fir("LON") == "EGTT"


def test_resolve_fir_prefers_sub_sector_prefix() -> None:
    reference = StaticReference.parse_vatspy(VATSPY)
    assert reference.resolve_fir("LON_C") == "EGTT-C"


def test_resolve_fir_unknown() -> None:
    reference = StaticReference.parse_vatspy(VATSPY)
    assert reference.resolve_fir("ZZZZ") is None
    assert reference.resolve_fir("") is None


def test_fir_for_airport() -> None:
    reference = StaticReference.parse_vatspy(VATSPY)
    assert reference.fir_for_airport("eddf") == "EDGG"
    assert reference.fir_for_airport("XXXX") is None


def test_boundaries_parse_and_lookup() -> None:
    boundaries = StaticReference.parse_boundaries(BOUNDARIES)
    reference = StaticReference(boundaries=boundaries)
    assert len(boundaries) == 3
    assert reference.fir_at(51.0, 1.0) == "EGTT"       # inside EGTT and EGPX
    assert reference.fir_at(54.0, 0.0) == "EGPX"       # only EGPX
    assert reference.fir_at(50.0, 8.5) == "EDGG"       # MultiPolygon
    assert reference.fir_at(0.0, 0.0) is None


def test_most_specific_polygon_wins() -> None:
    boundaries = StaticReference.parse_boundaries(BOUNDARIES)
    reference = StaticReference(boundaries=boundaries)
    # Both EGTT (2x2 deg) and EGPX (10x10 deg) contain this point.
    assert reference.fir_at(51.0, 1.0) == "EGTT"


def test_boundaries_without_geometry_are_skipped() -> None:
    payload = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"id": "XXXX"}, "geometry": None},
            {
                "type": "Feature",
                "properties": {"id": "YYYY"},
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            },
        ],
    }
    assert StaticReference.parse_boundaries(payload) == []


def test_reference_without_boundaries_returns_none() -> None:
    assert StaticReference.parse_vatspy(VATSPY).fir_at(51.0, 1.0) is None


# --- real-world hierarchy: sub-sectors, orphans, and US short forms ---------------

SIMPLE_VATSPY = """[Airports]
KCLT|Charlotte Douglas|35.214|-80.943|CLT|KZTL|0
[FIRs]
EGTT|London|LON|EGTT
KZMA|Miami|MIA|KZMA
KZTL|Atlanta|ATL|KZTL
EGTT-C|London ACC (Central) - London|LON_C|EGTT-C
"""

SUBSECTOR_GEO = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"id": "EGTT"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0.0, 50.0], [2.0, 50.0], [2.0, 52.0], [0.0, 52.0], [0.0, 50.0]]],
            },
        },
        {
            "type": "Feature",
            "properties": {"id": "EGTT-C", "label_lat": 51.0, "label_lon": 1.0},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[0.5, 50.5], [1.5, 50.5], [1.5, 51.5], [0.5, 51.5], [0.5, 50.5]]
                ],
            },
        },
        {
            "type": "Feature",
            "properties": {"id": "EGTL", "label_lat": 51.2, "label_lon": 0.8},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[0.6, 50.6], [1.0, 50.6], [1.0, 51.0], [0.6, 51.0], [0.6, 50.6]]
                ],
            },
        },
    ],
}


def _hierarchy() -> StaticReference:
    base = StaticReference.parse_vatspy(SIMPLE_VATSPY)
    return StaticReference(
        airports=base.airports,
        firs=base.firs,
        boundaries=StaticReference.parse_boundaries(SUBSECTOR_GEO),
    )


def test_sub_sector_polygon_is_identified_precisely() -> None:
    reference = _hierarchy()
    boundary = reference.boundary_at(51.2, 1.2)
    assert boundary is not None
    assert boundary.code == "EGTT-C"
    # ...while the FIR reported to the user is the top-level one.
    assert reference.fir_at(51.2, 1.2) == "EGTT"


def test_parent_controller_covers_sub_sector_polygons() -> None:
    reference = _hierarchy()
    covered = reference.boundary_codes_for_fir("EGTT")
    assert {"EGTT", "EGTT-C", "EGTL"} <= covered
    # A sub-sector position covers only itself.
    assert "EGTT-C" in reference.boundary_codes_for_fir("EGTT-C")
    assert "EGTT" not in reference.boundary_codes_for_fir("EGTT-C")


def test_orphan_polygon_is_assigned_to_containing_fir() -> None:
    reference = _hierarchy()
    # EGTL appears in Boundaries.geojson but in no FIR list; its label point is
    # inside EGTT, so its traffic must accrue to London, not vanish.
    assert reference._ensure_owner()["EGTL"] == "EGTT"
    assert reference.fir_at(50.8, 0.8) == "EGTT"


def test_us_enroute_short_form_resolves() -> None:
    reference = _hierarchy()
    assert reference.resolve_fir("ZMA") == "KZMA"
    assert reference.resolve_fir("ZMA_CTR") == "KZMA"
    assert reference.resolve_fir("KZMA") == "KZMA"


def test_us_terminal_short_form_resolves_through_airport() -> None:
    reference = _hierarchy()
    airport = reference.airport_for_stem("CLT_APP")
    assert airport is not None and airport.icao == "KCLT"
    assert reference.resolve_fir("CLT_APP") == "KZTL"


def test_european_icao_airport_still_resolves() -> None:
    reference = StaticReference.parse_vatspy(VATSPY)
    assert reference.resolve_fir("EDDF_TWR") == "EDGG"
    assert reference.airport_for_stem("EDDF") is not None
    assert reference.airport_for_stem("XXX") is None
