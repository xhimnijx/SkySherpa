"""Spherical geometry and polygon maths. Small, dependency-free, testable.

Distances are nautical miles; angles are degrees; GeoJSON coordinates are
``[lon, lat]`` (note the order, it is the classic source of silent bugs).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "BoundingBox",
    "destination",
    "distance_nm",
    "initial_bearing",
    "interpolate",
    "point_in_geometry",
    "point_in_ring",
    "simplify_rdp",
]

EARTH_RADIUS_NM = 3440.065
Point = tuple[float, float]  # (lat, lon)


def distance_nm(a: Point, b: Point) -> float:
    """Great-circle distance between two ``(lat, lon)`` points, in nautical miles."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(h)))


def initial_bearing(a: Point, b: Point) -> float:
    """Initial true bearing from *a* to *b*, degrees in ``[0, 360)``."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination(point: Point, bearing_deg: float, distance: float) -> Point:
    """Point reached by travelling *distance* nm from *point* on *bearing_deg*."""
    lat1, lon1 = math.radians(point[0]), math.radians(point[1])
    brg = math.radians(bearing_deg)
    angular = distance / EARTH_RADIUS_NM
    sin_lat2 = math.sin(lat1) * math.cos(angular) + math.cos(lat1) * math.sin(angular) * math.cos(brg)
    lat2 = math.asin(max(-1.0, min(1.0, sin_lat2)))
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * sin_lat2,
    )
    return (math.degrees(lat2), (math.degrees(lon2) + 540.0) % 360.0 - 180.0)


def interpolate(a: Point, b: Point, max_segment_nm: float) -> list[Point]:
    """Great-circle interpolation between two points, samples no further apart than *max_segment_nm*."""
    if max_segment_nm <= 0:
        raise ValueError("max_segment_nm must be positive")
    total = distance_nm(a, b)
    if total <= max_segment_nm:
        return [a, b]
    steps = max(1, math.ceil(total / max_segment_nm))
    brg = initial_bearing(a, b)
    # Endpoints are returned verbatim: callers compare against the origin/destination
    # exactly, and recomputing them through the spherical formula loses the last bits.
    return [a] + [destination(a, brg, total * i / steps) for i in range(1, steps)] + [b]


@dataclass(frozen=True, slots=True)
class BoundingBox:
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    @classmethod
    def from_points(cls, points: Iterable[Point]) -> BoundingBox:
        lats: list[float] = []
        lons: list[float] = []
        for lat, lon in points:
            lats.append(lat)
            lons.append(lon)
        if not lats:
            raise ValueError("empty point set")
        return cls(min(lats), min(lons), max(lats), max(lons))

    def contains(self, lat: float, lon: float) -> bool:
        return self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon

    def overlaps(self, other: BoundingBox) -> bool:
        return not (
            self.max_lat < other.min_lat
            or other.max_lat < self.min_lat
            or self.max_lon < other.min_lon
            or other.max_lon < self.min_lon
        )


def point_in_ring(lat: float, lon: float, ring: Sequence[Sequence[float]]) -> bool:
    """Ray casting. *ring* is a GeoJSON linear ring: ``[[lon, lat], ...]``."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def point_in_geometry(lat: float, lon: float, geometry: dict) -> bool:
    """Point-in-polygon for a GeoJSON ``Polygon``/``MultiPolygon`` dict."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        return _point_in_polygon(lat, lon, coords)
    if gtype == "MultiPolygon":
        return any(_point_in_polygon(lat, lon, poly) for poly in coords)
    return False


def _point_in_polygon(lat: float, lon: float, rings: Sequence[Sequence[Sequence[float]]]) -> bool:
    if not rings:
        return False
    if not point_in_ring(lat, lon, rings[0]):
        return False
    # Holes: inside an outer ring and inside a hole means outside.
    return not any(point_in_ring(lat, lon, hole) for hole in rings[1:])


def _xy(point: Point, lat0: float) -> tuple[float, float]:
    """Local equirectangular projection to nm; adequate for simplification tolerances."""
    return (point[1] * 60.0 * math.cos(math.radians(lat0)), point[0] * 60.0)


def _perpendicular_nm(point: Point, start: Point, end: Point, lat0: float) -> float:
    px, py = _xy(point, lat0)
    ax, ay = _xy(start, lat0)
    bx, by = _xy(end, lat0)
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def rdp_keep_indices(points: Sequence[Point], tolerance_nm: float) -> list[int]:
    """Indices retained by Ramer-Douglas-Peucker. Preserves first and last."""
    if tolerance_nm <= 0 or len(points) < 3:
        return list(range(len(points)))
    lat0 = sum(p[0] for p in points) / len(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True

    stack: list[tuple[int, int]] = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        worst = 0.0
        worst_index = first
        for i in range(first + 1, last):
            d = _perpendicular_nm(points[i], points[first], points[last], lat0)
            if d > worst:
                worst, worst_index = d, i
        if worst > tolerance_nm:
            keep[worst_index] = True
            stack.append((first, worst_index))
            stack.append((worst_index, last))

    return [i for i, k in enumerate(keep) if k]


def simplify_rdp(points: Sequence[Point], tolerance_nm: float) -> list[Point]:
    """Ramer-Douglas-Peucker simplification. Preserves endpoints."""
    return [points[i] for i in rdp_keep_indices(points, tolerance_nm)]
