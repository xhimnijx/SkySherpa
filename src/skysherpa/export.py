"""Publish a static bundle: the forecast, the geometry, and the catalogue.

The insight that makes a browser-side dispatcher possible is that the *model* does not
need to run in the browser. It runs once, nightly, and ships its output: a probability per
FIR per hour of the week, a probability per airport, a lookup grid, and the polygons to
draw. The browser then only does arithmetic on numbers it already has, which is why a
worldwide plan takes tens of milliseconds instead of a round trip.

Everything here is sized for a static host: roughly 1 MB gzipped, no database, no API.
"""

from __future__ import annotations

import json
import os
import struct
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable

from .aircraft import load_catalog
from .geo import BoundingBox, distance_nm
from .oracle import CoverageModel, PresenceMatrix, latest_calibrator
from .staticdata import Boundary, StaticReference
from .store import Database

__all__ = ["BundleResult", "build_bundle", "quantise"]

HOURS_PER_WEEK = 168
#: Degrees per cell in the FIR lookup grid. One degree is ~60 nm, comfortably smaller
#: than any FIR we care about and a single O(1) lookup on the client.
GRID_RESOLUTION = 1.0


@dataclass(slots=True)
class BundleResult:
    out_dir: Path
    files: dict[str, int] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(self.files.values())

    def summary(self) -> str:
        lines = [f"bundle written to {self.out_dir} ({self.total_bytes / 1024:.0f} KB)"]
        for name, size in sorted(self.files.items(), key=lambda item: -item[1]):
            lines.append(f"  {name:<26} {size / 1024:7.1f} KB")
        return "\n".join(lines)


def quantise(probability: float) -> int:
    """Map [0,1] to a byte. One part in 255 is far finer than the model's own error."""
    return max(0, min(255, int(round(probability * 255))))


def _hourly_week(model: CoverageModel, position: str) -> list[int]:
    """672 quarter-hour cells -> 168 hourly cells, as bytes.

    Planning granularity is an hour. Exporting quarter-hours would quadruple the bundle
    to buy a precision the forecast does not have.
    """
    out: list[int] = []
    for weekday in range(7):
        for hour in range(24):
            total = 0.0
            for part in range(4):
                total += model.predict(
                    position, weekday, hour * 4 + part, with_interval=False
                ).p
            out.append(quantise(total / 4.0))
    return out


def _any_of(probabilities: Iterable[int]) -> int:
    """P(at least one of these positions online), from their byte probabilities."""
    uncovered = 1.0
    for value in probabilities:
        uncovered *= 1.0 - (value / 255.0)
    return quantise(1.0 - uncovered)


def _rings(geometry: dict[str, Any]) -> list[list[list[float]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        return [coords[0]] if coords else []
    if gtype == "MultiPolygon":
        return [part[0] for part in coords if part]
    return []


def _simplify_ring(points: list[list[float]], tolerance: float = 0.12) -> list[list[float]]:
    """Douglas-Peucker on lon/lat degrees; enough for a canvas outline, and 10x smaller.

    A GeoJSON ring closes on itself, which breaks the naive version: the segment between
    the first and last point is degenerate, every perpendicular distance is zero, and the
    whole polygon collapses to two points. Strip the duplicate closing vertex, simplify the
    open polyline, then close it again.
    """
    if len(points) < 4:
        return [[round(p[0], 2), round(p[1], 2)] for p in points]

    closed = points[0][0] == points[-1][0] and points[0][1] == points[-1][1]
    work = points[:-1] if closed else points
    if len(work) < 3:
        return [[round(p[0], 2), round(p[1], 2)] for p in points]

    keep = [False] * len(work)
    keep[0] = keep[-1] = True
    stack = [(0, len(work) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        x0, y0 = work[first]
        x1, y1 = work[last]
        dx, dy = x1 - x0, y1 - y0
        norm = (dx * dx + dy * dy) ** 0.5 or 1e-9
        worst, worst_index = 0.0, first
        for index in range(first + 1, last):
            px, py = work[index]
            dist = abs(dy * px - dx * py + x1 * y0 - y1 * x0) / norm
            if dist > worst:
                worst, worst_index = dist, index
        if worst > tolerance:
            keep[worst_index] = True
            stack.append((first, worst_index))
            stack.append((worst_index, last))

    simplified = [work[i] for i, k in enumerate(keep) if k]
    if closed:
        simplified.append(simplified[0])
    return [[round(p[0], 2), round(p[1], 2)] for p in simplified]


def _rasterise(
    boundaries: list[Boundary], index_of: dict[str, int], resolution: float = GRID_RESOLUTION
) -> bytes:
    """Burn FIR polygons into a lat/lon grid so the client can answer 'which FIR?' in O(1).

    Scanline fill, largest polygon first so that the most specific airspace wins. Without
    this the browser would have to run point-in-polygon against a thousand rings for every
    route sample, which is the difference between 40 ms and 4 seconds.
    """
    width = int(360 / resolution)
    height = int(180 / resolution)
    grid = [0] * (width * height)

    for boundary in sorted(boundaries, key=lambda item: item.area_hint, reverse=True):
        value = index_of.get(boundary.code)
        if not value:
            continue
        for ring in _rings(boundary.geometry):
            if len(ring) < 4:
                continue
            lats = [point[1] for point in ring]
            row_min = max(0, int((min(lats) + 90) / resolution))
            row_max = min(height - 1, int((max(lats) + 90) / resolution))
            for row in range(row_min, row_max + 1):
                lat = -90 + (row + 0.5) * resolution
                crossings: list[float] = []
                for index in range(len(ring) - 1):
                    x1, y1 = ring[index][0], ring[index][1]
                    x2, y2 = ring[index + 1][0], ring[index + 1][1]
                    if (y1 > lat) != (y2 > lat):
                        crossings.append(x1 + (lat - y1) * (x2 - x1) / (y2 - y1))
                if len(crossings) < 2:
                    continue
                crossings.sort()
                for pair in range(0, len(crossings) - 1, 2):
                    start = max(0, int((crossings[pair] + 180) / resolution))
                    end = min(width - 1, int((crossings[pair + 1] + 180) / resolution))
                    base = row * width
                    for column in range(start, end + 1):
                        grid[base + column] = value

    return struct.pack(f"<{len(grid)}H", *grid)


def _airport_traffic(db: Database) -> dict[str, int]:
    traffic: dict[str, int] = {}
    for row in db.query(
        "SELECT dep_icao AS icao, SUM(n_flights) AS n FROM pair_stat GROUP BY dep_icao"
    ):
        traffic[str(row["icao"])] = traffic.get(str(row["icao"]), 0) + int(row["n"] or 0)
    for row in db.query(
        "SELECT arr_icao AS icao, SUM(n_flights) AS n FROM pair_stat GROUP BY arr_icao"
    ):
        traffic[str(row["icao"])] = traffic.get(str(row["icao"]), 0) + int(row["n"] or 0)
    return traffic


def build_bundle(
    db: Database,
    *,
    out_dir: Path | str,
    reference: StaticReference | None = None,
    reference_date: date | None = None,
    max_pairs: int = 20_000,
    max_airports: int = 2_500,
    write_web_assets: bool = True,
    calibrate: bool = True,
) -> BundleResult:
    out = Path(out_dir)
    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    matrix = PresenceMatrix.from_db(db)
    if not matrix.days:
        raise RuntimeError("no presence data; run `skysherpa rollup` first")
    model = CoverageModel.fit(matrix, reference_date=reference_date or date.today())

    # Recalibrate against a genuine out-of-sample holdout before exporting. This took the
    # measured calibration error from 0.036 to 0.016 on the synthetic network; the browser
    # only ever sees the corrected numbers.
    calibrator = latest_calibrator(matrix) if calibrate else None
    if calibrator is not None and calibrator.knots:
        model.calibrate_with(calibrator)

    # Which positions are en-route, and where
    position_fir: dict[str, str] = {}
    position_airport: dict[str, str] = {}
    facility_of: dict[str, str] = {}
    for row in db.query("SELECT position_key, fir_code, airport_icao, facility FROM position"):
        key = str(row["position_key"])
        facility_of[key] = str(row["facility"])
        if row["fir_code"]:
            position_fir[key] = str(row["fir_code"]).upper()
        if row["airport_icao"]:
            position_airport[key] = str(row["airport_icao"]).upper()

    modelled = set(model.positions)

    # ---- en-route probabilities: control and flight service, separately ------
    fir_codes: set[str] = set(position_fir.values())
    if reference is not None:
        fir_codes |= {code.split("-")[0] for code in reference.firs}
    fir_list = sorted(fir_codes)
    fir_index = {code: i + 1 for i, code in enumerate(fir_list)}  # grid uses 1-based

    def _combine(positions: list[str]) -> list[int]:
        if not positions:
            return [0] * HOURS_PER_WEEK
        joined = [_hourly_week(model, position) for position in positions]
        return [_any_of(cell[hour] for cell in joined) for hour in range(HOURS_PER_WEEK)]

    by_fir: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for position, fir in position_fir.items():
        facility = facility_of.get(position)
        if position in modelled and facility in ("CTR", "FSS"):
            by_fir[fir][facility].append(position)

    fir_ctr = {code: _combine(by_fir[code]["CTR"]) for code in fir_list}
    fir_fss = {code: _combine(by_fir[code]["FSS"]) for code in fir_list}

    # ---- airport service stack ---------------------------------------------
    # The layers are nested, because a controller working top-down provides every
    # layer beneath them: a centre covers approach, tower, ground *and* delivery.
    # So each layer is the union of its own positions and the layer above it, which
    # is also why ground coverage can never exceed tower coverage, and why a tower
    # alone is a fundamentally different experience from a full stack.
    by_airport: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for position, airport in position_airport.items():
        facility = facility_of.get(position)
        if position in modelled and facility in ("DEL", "GND", "TWR", "APP"):
            by_airport[airport][facility].append(position)

    def _union(lower: list[int], upper: list[int]) -> list[int]:
        return [
            quantise(1.0 - (1.0 - a / 255.0) * (1.0 - b / 255.0))
            for a, b in zip(lower, upper)
        ]

    zeros = [0] * HOURS_PER_WEEK
    traffic = _airport_traffic(db)
    candidates = set(by_airport) | set(traffic)
    airports: list[dict[str, Any]] = []
    airport_layers: dict[str, list[list[int]]] = {}
    if reference is not None:
        for icao in sorted(candidates, key=lambda code: -traffic.get(code, 0))[:max_airports]:
            airport = reference.airports.get(icao)
            if airport is None:
                continue
            fir = (airport.fir or "").upper()
            centre = fir_ctr.get(fir, zeros)
            approach = _union(_combine(by_airport[icao]["APP"]), centre)
            tower = _union(_combine(by_airport[icao]["TWR"]), approach)
            ground = _union(_combine(by_airport[icao]["GND"]), tower)
            delivery = _union(_combine(by_airport[icao]["DEL"]), ground)
            airport_layers[icao] = [delivery, ground, tower, approach]
            # Which layers this airport has ever been seen to staff, as a bitmask.
            # Presets that renormalise (GA) use it to stop penalising a grass strip for
            # not having a delivery position it never had.
            stack = 0
            for index, layer in enumerate(airport_layers[icao]):
                if max(layer) >= 50:  # ~20% at some point in the week
                    stack |= 1 << index
            airports.append(
                {
                    "icao": icao,
                    "name": airport.name,
                    "lat": round(airport.lat, 4),
                    "lon": round(airport.lon, 4),
                    "iata": airport.iata,
                    "fir": airport.fir,
                    "traffic": traffic.get(icao, 0),
                    "stack": stack,
                }
            )

    # ---- city pairs --------------------------------------------------------
    pairs = []
    known = {airport["icao"]: airport for airport in airports}
    for row in db.query(
        "SELECT dep_icao, arr_icao, n_flights, med_block_min, distance_nm FROM pair_stat "
        "ORDER BY n_flights DESC LIMIT ?",
        (max_pairs,),
    ):
        dep, arr = str(row["dep_icao"]), str(row["arr_icao"])
        if dep not in known or arr not in known:
            continue
        distance = row["distance_nm"]
        if distance is None:
            a, b = known[dep], known[arr]
            distance = round(distance_nm((a["lat"], a["lon"]), (b["lat"], b["lon"])), 1)
        pairs.append(
            {
                "d": dep,
                "a": arr,
                "n": int(row["n_flights"] or 0),
                "m": int(row["med_block_min"]) if row["med_block_min"] is not None else None,
                "nm": round(float(distance), 1),
            }
        )

    # ---- geometry ----------------------------------------------------------
    polygons: list[dict[str, Any]] = []
    if reference is not None:
        for boundary in reference.boundaries:
            value = fir_index.get(boundary.code) or fir_index.get(boundary.code.split("-")[0])
            for ring in _rings(boundary.geometry):
                simplified = _simplify_ring(ring)
                if len(simplified) >= 4:
                    polygons.append({"id": value or 0, "pts": simplified})

    grid = _rasterise(reference.boundaries if reference else [], fir_index)

    # ---- aircraft catalogue ------------------------------------------------
    catalog = load_catalog()
    aircraft = [
        {
            "icao": t.icao,
            "name": t.name,
            "class": t.aircraft_class,
            "range_nm": t.range_nm,
            "tas": t.cruise_tas_kt,
            "fl": t.cruise_fl,
            "runway": t.min_runway_ft,
            "climb": t.climb_fpm,
            "descent": t.descent_fpm,
            "taxi": t.taxi_min,
            "wake": t.wake,
            "engines": t.engines,
        }
        for t in catalog.types.values()
    ]

    # ---- confidence: how much history sits behind each number -------------------
    # The first version exported n_eff, the model's per-cell sample size. It is the
    # statistically precise figure and it is useless in a UI: with 56 days of history a
    # given (weekday, hour) cell still only holds ~8 observations, so every badge read
    # "6d" and looked broken. What a user wants to know is "have you been watching this
    # airspace for a day or a year", so export the observed window instead.
    def _observed_days(position_keys: list[str]) -> int:
        days: set = set()
        for position in position_keys:
            for day, _slot, _online in matrix.days.get(position, ()):
                days.add(day)
        return min(255, len(days))

    fir_days = [
        _observed_days(by_fir[code]["CTR"] + by_fir[code]["FSS"]) for code in fir_list
    ]
    airport_days = [
        _observed_days(
            by_airport[airport["icao"]]["DEL"]
            + by_airport[airport["icao"]]["GND"]
            + by_airport[airport["icao"]]["TWR"]
            + by_airport[airport["icao"]]["APP"]
        )
        for airport in airports
    ]

    generated = datetime.now(UTC)
    meta = {
        "version": 1,
        "generated_at": generated.isoformat(),
        "hours_per_week": HOURS_PER_WEEK,
        "grid": {"resolution": GRID_RESOLUTION, "width": 360, "height": 180},
        "fir_count": len(fir_list),
        "airport_count": len(airports),
        "pair_count": len(pairs),
        # Terminal layers, shallow to deep. Each is the probability that *this* service is
        # available, including top-down cover from the layer above.
        "layers": ["del", "gnd", "twr", "app"],
        "airport_layer_bytes": 4 * HOURS_PER_WEEK,
        # Observed-history depth behind each FIR/airport probability, in days.
        "confidence": {"unit": "days"},
        "positions_modelled": len(model.positions),
        "days_observed": len({d for rows in matrix.days.values() for d, _, _ in rows}),
        "half_life_days": model.params.half_life_days,
        "calibration": {
            "applied": bool(calibrator and calibrator.knots),
            "knots": len(calibrator.knots) if calibrator else 0,
        },
        "source": "skysherpa",
    }

    files: dict[str, int] = {}
    written_files: set[Path] = set()

    def _atomic_write(path: Path, data: bytes) -> None:
        """Write to a sibling temp file, then rename.

        The publisher rewrites ~750 KB every fifteen minutes while browsers are reading
        it. An in-place write can be observed half-finished, which shows up as a JSON
        parse error or a truncated typed array -- an intermittent, unreproducible bug.
        os.replace is atomic on the same filesystem, including Windows.
        """
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_bytes(data)
        os.replace(temp, path)
        written_files.add(path)
        files[path.relative_to(out).as_posix()] = path.stat().st_size

    def write_json(name: str, payload: Any) -> None:
        _atomic_write(
            data_dir / name,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )

    def write_binary(name: str, payload: bytes) -> None:
        _atomic_write(data_dir / name, payload)

    write_json("meta.json", meta)
    write_json("firs.json", [{"code": code} for code in fir_list])
    write_binary("fir_ctr.bin", bytes(value for code in fir_list for value in fir_ctr[code]))
    write_binary("fir_fss.bin", bytes(value for code in fir_list for value in fir_fss[code]))
    write_binary("fir_grid.bin", grid)
    write_json("airports.json", airports)
    write_binary(
        "airport_layers.bin",
        bytes(
            value
            for airport in airports
            for layer in airport_layers[airport["icao"]]
            for value in layer
        ),
    )
    write_json("pairs.json", pairs)
    write_json("aircraft.json", aircraft)
    write_json("polygons.json", polygons)
    write_binary("fir_days.bin", bytes(fir_days))
    write_binary("airport_days.bin", bytes(airport_days))
    # Position index: lets the browser map a live controller callsign to the FIR or
    # airport it serves, so the forecast can be anchored to who is online right now.
    write_json(
        "positions.json",
        [
            {
                "k": key,
                "f": position_fir.get(key, ""),
                "a": position_airport.get(key, ""),
                "t": facility_of.get(key, ""),
            }
            for key in sorted(facility_of)
        ],
    )

    if write_web_assets:
        web_dir = Path(__file__).resolve().parents[2] / "web"
        if web_dir.is_dir():
            for asset in ("index.html", "app.js", "core.js", "style.css"):
                source = web_dir / asset
                if source.is_file():
                    _atomic_write(out / asset, source.read_bytes())

    # Remove anything left over from an older bundle layout. Without this the deploy
    # directory accumulates dead files (fir_prob.bin from before the layer split, say) and
    # nobody notices until the bundle is twice the size it should be. Compared against the
    # set of paths actually written -- a path-prefix test looked green on Linux and deleted
    # every file on Windows, where relative paths use backslashes.
    for stale in data_dir.iterdir():
        if stale.is_file() and stale not in written_files and not stale.name.endswith(".tmp"):
            stale.unlink()

    return BundleResult(out_dir=out, files=files)
