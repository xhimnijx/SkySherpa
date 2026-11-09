"""Geometry tests: great-circle maths, RDP simplification, point-in-polygon."""

from __future__ import annotations

import math

import pytest

from skysherpa.geo import (
    BoundingBox,
    destination,
    distance_nm,
    initial_bearing,
    interpolate,
    point_in_geometry,
    point_in_ring,
    simplify_rdp,
)

LHR = (51.4775, -0.461389)
AMS = (52.3105, 4.7683)
JFK = (40.6413, -73.7781)


def test_zero_distance() -> None:
    assert distance_nm(LHR, LHR) == pytest.approx(0.0, abs=1e-9)


def test_known_distance_lhr_ams() -> None:
    # Published great-circle: ~200 nm.
    assert distance_nm(LHR, AMS) == pytest.approx(200, abs=8)


def test_known_distance_lhr_jfk() -> None:
    # Published great-circle: ~3,000 nm.
    assert distance_nm(LHR, JFK) == pytest.approx(3_000, abs=60)


def test_bearing_eastbound() -> None:
    # LHR -> AMS is east and slightly north; the great-circle initial bearing is ~74.
    bearing = initial_bearing(LHR, AMS)
    assert 68 < bearing < 85


def test_bearing_to_west_is_roughly_west() -> None:
    bearing = initial_bearing(LHR, JFK)
    assert 270 < bearing < 300


def test_destination_round_trip() -> None:
    out = destination(LHR, 90.0, 60.0)
    assert distance_nm(LHR, out) == pytest.approx(60.0, abs=0.05)


def test_destination_north_changes_latitude_only() -> None:
    north = destination(LHR, 0.0, 60.0)
    assert north[1] == pytest.approx(LHR[1], abs=0.01)
    assert north[0] > LHR[0]


def test_interpolate_spacing_and_endpoints() -> None:
    points = interpolate(LHR, JFK, 100.0)
    assert points[0] == LHR
    assert points[-1] == JFK
    for a, b in zip(points, points[1:]):
        assert distance_nm(a, b) <= 100.0 + 1e-6


def test_interpolate_short_segment_returns_endpoints() -> None:
    assert interpolate(LHR, AMS, 500.0) == [LHR, AMS]


def test_bounding_box() -> None:
    box = BoundingBox.from_points([LHR, AMS, JFK])
    assert box.contains(*AMS)
    assert not box.contains(0.0, 0.0)
    assert box.overlaps(BoundingBox(50, -1, 53, 5))


def test_point_in_ring_square() -> None:
    square = [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
    assert point_in_ring(1.0, 1.0, square)
    assert not point_in_ring(3.0, 1.0, square)
    assert not point_in_ring(1.0, -1.0, square)


def test_point_in_polygon_with_hole() -> None:
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
        ],
    }
    assert point_in_geometry(1.0, 1.0, polygon)
    assert not point_in_geometry(5.0, 5.0, polygon)  # inside the hole
    assert not point_in_geometry(20.0, 20.0, polygon)


def test_point_in_multipolygon() -> None:
    multi = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
            [[[10, 10], [12, 10], [12, 12], [10, 12], [10, 10]]],
        ],
    }
    assert point_in_geometry(0.5, 0.5, multi)
    assert point_in_geometry(11.0, 11.0, multi)
    assert not point_in_geometry(5.0, 5.0, multi)


def test_point_in_geometry_ignores_unsupported_types() -> None:
    assert not point_in_geometry(0.0, 0.0, {"type": "LineString", "coordinates": []})


def test_rdp_collapses_a_straight_line() -> None:
    points = [(0.0, lon / 60.0) for lon in range(11)]
    assert len(simplify_rdp(points, 0.1)) == 2


def test_rdp_keeps_a_real_deviation() -> None:
    points = [(0.0, 0.0), (0.0, 0.5), (0.5, 1.0), (0.0, 1.5), (0.0, 2.0)]
    simplified = simplify_rdp(points, 0.2)
    assert (0.5, 1.0) in simplified
    assert simplified[0] == points[0]
    assert simplified[-1] == points[-1]


def test_rdp_no_tolerance_returns_input() -> None:
    points = [(0.0, 0.0), (0.1, 0.1), (0.0, 1.0)]
    assert simplify_rdp(points, 0.0) == points


def test_rdp_requires_two_or_more_points() -> None:
    assert simplify_rdp([(0.0, 0.0)], 0.5) == [(0.0, 0.0)]


def test_distance_is_symmetric() -> None:
    assert distance_nm(LHR, AMS) == pytest.approx(distance_nm(AMS, LHR), abs=1e-9)


def test_distance_never_exceeds_half_circumference() -> None:
    antipode = (LHR[0] - 0.0, LHR[1] + 180.0)
    assert distance_nm(LHR, antipode) <= math.pi * 3440.065 + 1e-6
