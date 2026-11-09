"""Static reference data: airports/firs from VATSpy.dat, FIR polygons from GeoJSON.

Kept separate from the live feed because it changes weekly, not every 30 seconds,
and because it is the piece that turns a callsign or a coordinate into a *place*
that the coverage model can reason about.

Two real-world wrinkles drive the design:

* ``Boundaries.geojson`` mixes FIRs (``KZMA``, ``EGTT``) with hundreds of
  sub-sectors (``KZMA-N``, ``EGTT-C``) and a few polygons that exist in neither list
  (``EGTL``, the London TMA). Traffic must be aggregated to an *owning FIR*, which we
  resolve by testing each polygon's label point against the FIR polygons.
* US callsigns drop the ICAO prefix: Miami Center logs on as ``ZMA_CTR`` for FIR
  ``KZMA``, and Charlotte Tower as ``CLT_APP`` for airport ``KCLT``. Without those
  rules, roughly half of North American staffing silently disappears.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import net
from .config import ENDPOINTS, Settings
from .geo import BoundingBox, point_in_geometry

__all__ = ["Airport", "Boundary", "Fir", "StaticReference", "load_reference"]

_FRESH_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class Airport:
    icao: str
    name: str
    lat: float
    lon: float
    iata: str = ""
    fir: str = ""


@dataclass(frozen=True, slots=True)
class Fir:
    code: str
    name: str
    prefix: str = ""
    callsign: str = ""


@dataclass(frozen=True, slots=True)
class Boundary:
    code: str
    bbox: BoundingBox
    geometry: dict[str, Any]
    label_lat: float | None = None
    label_lon: float | None = None

    @property
    def area_hint(self) -> float:
        """Bounding-box area, used only to prefer the most specific polygon."""
        return (self.bbox.max_lat - self.bbox.min_lat) * (self.bbox.max_lon - self.bbox.min_lon)


class StaticReference:
    """Airports, FIRs, and the polygons that turn a position into a jurisdiction."""

    def __init__(
        self,
        airports: dict[str, Airport] | None = None,
        firs: dict[str, Fir] | None = None,
        boundaries: list[Boundary] | None = None,
    ) -> None:
        self.airports = airports or {}
        self.firs = firs or {}
        self._boundaries = boundaries or []
        self._prefix_index: dict[str, str] = {}
        self._short_fir_index: dict[str, str] = {}
        self._lat_bands: dict[int, list[Boundary]] = {}
        self._owner: dict[str, str] | None = None
        self._containing: dict[str, set[str]] = {}

        for code, fir in self.firs.items():
            for key in (fir.prefix, fir.callsign, code):
                if key:
                    self._prefix_index.setdefault(key.upper(), code)
            # `ZMA_CTR` -> KZMA: US enroute callsigns omit the leading K.
            if len(code) == 4 and code[0] in ("K", "P", "C"):
                self._short_fir_index.setdefault(code[1:], code)
        for boundary in self._boundaries:
            lo = math.floor(boundary.bbox.min_lat)
            hi = math.floor(boundary.bbox.max_lat)
            for band in range(lo, hi + 1):
                self._lat_bands.setdefault(band, []).append(boundary)

    # ---- parsing -------------------------------------------------------

    @classmethod
    def parse_vatspy(cls, text: str) -> StaticReference:
        airports: dict[str, Airport] = {}
        firs: dict[str, Fir] = {}
        section = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().lower()
                continue
            parts = line.split("|")
            if section == "airports" and len(parts) >= 6:
                icao = parts[0].strip().upper()
                if len(icao) != 4:
                    continue
                try:
                    lat = float(parts[2])
                    lon = float(parts[3])
                except ValueError:
                    continue
                airports[icao] = Airport(
                    icao=icao,
                    name=parts[1].strip(),
                    lat=lat,
                    lon=lon,
                    iata=parts[4].strip().upper(),
                    fir=parts[5].strip().upper(),
                )
            elif section in ("firs", "uirs") and len(parts) >= 3:
                code = parts[0].strip().upper()
                if not code:
                    continue
                firs.setdefault(
                    code,
                    Fir(
                        code=code,
                        name=parts[1].strip(),
                        prefix=parts[2].strip().upper(),
                        callsign=parts[3].strip().upper() if len(parts) > 3 else "",
                    ),
                )
        return cls(airports=airports, firs=firs)

    @classmethod
    def parse_boundaries(cls, payload: dict[str, Any]) -> list[Boundary]:
        out: list[Boundary] = []
        for feature in payload.get("features", []):
            props = feature.get("properties") or {}
            code = str(props.get("id") or props.get("ID") or props.get("code") or "").upper()
            geometry = feature.get("geometry") or {}
            if not code or geometry.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            rings = _iter_rings(geometry)
            points = [pt for ring in rings for pt in ring]
            if not points:
                continue
            bbox = BoundingBox.from_points([(pt[1], pt[0]) for pt in points])
            out.append(
                Boundary(
                    code=code,
                    bbox=bbox,
                    geometry=geometry,
                    label_lat=_optional_float(props.get("label_lat")),
                    label_lon=_optional_float(props.get("label_lon")),
                )
            )
        return out

    # ---- firewall ------------------------------------------------------

    def _ensure_owner(self) -> dict[str, str]:
        """Map every boundary code to the FIR that owns it.

        Sub-sectors map to their parent when the parent is a known FIR; polygons that
        exist in neither list (``EGTL``) are assigned by testing their label point
        against the FIR polygons. A dozen extra milliseconds, once, at startup.
        """
        if self._owner is not None:
            return self._owner
        if not self.firs:
            # No FIR list to resolve against: the polygon code is the best answer we
            # have, and returning None would make every lookup useless.
            self._owner = {b.code: b.code for b in self._boundaries}
            return self._owner
        fir_codes = set(self.firs)
        fir_polygons = [b for b in self._boundaries if b.code in fir_codes]
        owner: dict[str, str] = {}
        containing: dict[str, set[str]] = {}

        for boundary in self._boundaries:
            inside: set[str] = set()
            if boundary.label_lat is not None and boundary.label_lon is not None:
                for candidate in fir_polygons:
                    if not candidate.bbox.contains(boundary.label_lat, boundary.label_lon):
                        continue
                    if point_in_geometry(boundary.label_lat, boundary.label_lon, candidate.geometry):
                        inside.add(candidate.code)
            if inside:
                # A polygon that sits geometrically inside other FIRs is covered by
                # whichever of them is staffed (the London TMA inside EGTT, say).
                containing[boundary.code] = inside

            if boundary.code in fir_codes:
                owner[boundary.code] = boundary.code
                continue
            resolved: str | None = None
            if inside:
                # An orphan polygon belongs to the top-level FIR, not to whichever
                # sub-sector happens to contain its label.
                resolved = max(inside, key=lambda code: _area(fir_polygons, code))
            if resolved is None:
                parent = boundary.code.split("-")[0]
                if parent in fir_codes:
                    resolved = parent
            if resolved is not None:
                owner[boundary.code] = resolved

        self._containing = containing
        self._owner = owner
        return owner

    # ---- lookup --------------------------------------------------------

    @property
    def boundaries(self) -> list[Boundary]:
        """Every polygon the reference knows about, in file order."""
        return self._boundaries

    def boundary_at(self, lat: float, lon: float) -> Boundary | None:
        """The most specific polygon containing a coordinate."""
        candidates = [
            b for b in self._lat_bands.get(math.floor(lat), []) if b.bbox.contains(lat, lon)
        ]
        best: Boundary | None = None
        for boundary in candidates:
            if point_in_geometry(lat, lon, boundary.geometry):
                if best is None or boundary.area_hint < best.area_hint:
                    best = boundary
        return best

    def fir_at(self, lat: float, lon: float) -> str | None:
        """Top-level FIR owning a coordinate."""
        boundary = self.boundary_at(lat, lon)
        if boundary is None:
            return None
        owned = self._ensure_owner().get(boundary.code)
        return self.root_fir(owned) if owned else None

    @staticmethod
    def root_fir(code: str) -> str:
        """``KZMA-N`` -> ``KZMA``, ``EGTT-C`` -> ``EGTT``, ``EGTT`` -> ``EGTT``."""
        return code.split("-")[0]

    def boundary_codes_for_fir(self, fir_code: str) -> set[str]:
        """Every polygon a controller on *fir_code* can be responsible for.

        A parent position (``LON_CTR``) works all of its sub-sectors; a sub-sector
        position (``LON_C_CTR``) works only its own. That hierarchy is what makes
        top-down coverage count correctly.
        """
        fir_code = fir_code.upper()
        owner = self._ensure_owner()
        covered: set[str] = set()
        for boundary in self._boundaries:
            code = boundary.code
            if (
                code == fir_code
                or self.root_fir(code) == fir_code
                or owner.get(code) == fir_code
                or fir_code in self._containing.get(code, ())
            ):
                covered.add(code)
        return covered

    def fir_for_airport(self, icao: str) -> str | None:
        airport = self.airports.get(icao.upper())
        return (airport.fir or None) if airport else None

    def airport_for_stem(self, stem: str) -> Airport | None:
        """Resolve a callsign stem to an airport, honouring US/Canadian short forms."""
        base = stem.upper().split("_")[0]
        if base in self.airports:
            return self.airports[base]
        if len(base) == 3:
            for prefix in ("K", "C", "P"):
                candidate = self.airports.get(prefix + base)
                if candidate is not None:
                    return candidate
        return None

    def resolve_fir(self, stem: str) -> str | None:
        """Map a callsign stem (``LON_S``, ``EGTT``, ``ZMA``, ``EDDF``) to an FIR code."""
        if not stem:
            return None
        stem = stem.upper()
        base = stem.split("_")[0]

        airport = self.airport_for_stem(stem)
        if airport is not None and airport.fir:
            return airport.fir

        for key in (stem, base):
            if key in self.firs:
                return key
            if key in self._prefix_index:
                return self._prefix_index[key]
            if key in self._short_fir_index:
                return self._short_fir_index[key]
        return None


def _area(polygons: list[Boundary], code: str) -> float:
    for boundary in polygons:
        if boundary.code == code:
            return boundary.area_hint
    return 0.0


def _optional_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _iter_rings(geometry: dict[str, Any]) -> list[list[list[float]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        return list(coords)
    if gtype == "MultiPolygon":
        return [ring for poly in coords for ring in poly]
    return []


def _cached(path: Path, max_age: float) -> bytes | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if time.time() - stat.st_mtime > max_age:
        return None
    return path.read_bytes()


def load_reference(settings: Settings, *, refresh: bool = False) -> StaticReference:
    """Load (and cache) the static reference. Network is used only on a cache miss."""
    settings.ensure_dirs()
    dat_path = settings.cache_dir / "vatspy.dat"
    geo_path = settings.cache_dir / "boundaries.geojson"

    dat_bytes = None if refresh else _cached(dat_path, _FRESH_SECONDS)
    if dat_bytes is None:
        response = net.get(ENDPOINTS["vatspy_dat"], settings, accept="text/plain")
        if not response.ok:
            if dat_path.exists():
                dat_bytes = dat_path.read_bytes()
            else:
                raise RuntimeError(f"could not fetch VATSpy.dat: {response.error}")
        else:
            dat_bytes = response.body
            dat_path.write_bytes(dat_bytes)

    geo_bytes = None if refresh else _cached(geo_path, _FRESH_SECONDS)
    if geo_bytes is None:
        response = net.get(ENDPOINTS["boundaries"], settings, accept="application/json")
        if not response.ok:
            if geo_path.exists():
                geo_bytes = geo_path.read_bytes()
            else:
                # Airports/FIRs are still useful without polygons.
                return StaticReference.parse_vatspy(dat_bytes.decode("utf-8", "replace"))
        else:
            geo_bytes = response.body
            geo_path.write_bytes(geo_bytes)

    reference = StaticReference.parse_vatspy(dat_bytes.decode("utf-8", "replace"))
    try:
        boundaries = StaticReference.parse_boundaries(json.loads(geo_bytes))
    except (json.JSONDecodeError, UnicodeDecodeError):
        boundaries = []
    return StaticReference(
        airports=reference.airports, firs=reference.firs, boundaries=boundaries
    )
