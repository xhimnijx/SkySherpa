"""Bundle export tests: sizes, counts, and the lookup grid the browser depends on."""

from __future__ import annotations

import json
import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skysherpa.export import build_bundle, quantise
from skysherpa.geo import BoundingBox
from skysherpa.rollup import rebuild_all
from skysherpa.simulate import simulate_flights, simulate_network
from skysherpa.staticdata import Airport, Boundary, Fir, StaticReference
from skysherpa.store import Database


def _reference() -> StaticReference:
    polygon = {
        "type": "Polygon",
        "coordinates": [[[-2.0, 50.0], [2.0, 50.0], [2.0, 52.0], [-2.0, 52.0], [-2.0, 50.0]]],
    }
    return StaticReference(
        airports={
            "EGLL": Airport("EGLL", "London Heathrow", 51.4775, -0.4614, "LHR", "EGTT"),
            "LEPA": Airport("LEPA", "Palma de Mallorca", 39.5517, 2.7388, "PMI", "LECM"),
        },
        firs={
            "EGTT": Fir("EGTT", "London", "LON", "EGTT"),
            "LECM": Fir("LECM", "Madrid", "MAD", "LECM"),
        },
        boundaries=[
            Boundary(
                code="EGTT",
                bbox=BoundingBox(50.0, -2.0, 52.0, 2.0),
                geometry=polygon,
                label_lat=51.0,
                label_lon=0.0,
            )
        ],
    )


@pytest.fixture()
def bundle(tmp_path: Path) -> tuple[Path, dict]:
    database = Database(tmp_path / "export.sqlite3")
    database.initialize()
    end = datetime(2026, 11, 9, 22, tzinfo=UTC)
    simulate_network(database, days=21, seed=3, end=end)
    simulate_flights(database, days=21, seed=4, end=end)
    rebuild_all(database)

    out = tmp_path / "dist"
    build_bundle(
        database,
        out_dir=out,
        reference=_reference(),
        reference_date=end.date(),
        write_web_assets=False,
    )
    meta = json.loads((out / "data" / "meta.json").read_text(encoding="utf-8"))
    database.close()
    return out, meta


def test_meta_counts(bundle) -> None:
    out, meta = bundle
    assert meta["hours_per_week"] == 168
    assert meta["fir_count"] >= 2
    assert meta["airport_count"] == 2
    assert meta["days_observed"] == 21
    assert (out / "data" / "meta.json").exists()


def test_probability_arrays_have_the_right_length(bundle) -> None:
    out, meta = bundle
    data = out / "data"
    fir_bytes = (data / "fir_prob.bin").read_bytes()
    airport_bytes = (data / "airport_prob.bin").read_bytes()
    assert len(fir_bytes) == meta["fir_count"] * 168
    assert len(airport_bytes) == meta["airport_count"] * 168
    assert max(fir_bytes) <= 255


def test_grid_is_a_full_world(bundle) -> None:
    out, meta = bundle
    payload = (out / "data" / "fir_grid.bin").read_bytes()
    assert len(payload) == meta["grid"]["width"] * meta["grid"]["height"] * 2


def test_grid_maps_a_point_to_the_right_fir(bundle) -> None:
    out, _ = bundle
    firs = json.loads((out / "data" / "firs.json").read_text(encoding="utf-8"))
    index = {entry["code"]: position + 1 for position, entry in enumerate(firs)}
    grid = struct.unpack("<64800H", (out / "data" / "fir_grid.bin").read_bytes())

    def lookup(lat: float, lon: float) -> int:
        x = int(lon + 180)
        y = int(lat + 90)
        return grid[y * 360 + x]

    assert lookup(51.5, -0.5) == index["EGTT"]
    assert lookup(0.0, -150.0) == 0  # middle of the Pacific: no FIR


def test_airports_carry_probabilities_not_just_coordinates(bundle) -> None:
    out, _ = bundle
    airports = json.loads((out / "data" / "airports.json").read_text(encoding="utf-8"))
    heathrow = next(item for item in airports if item["icao"] == "EGLL")
    assert heathrow["lat"] == pytest.approx(51.4775)
    assert heathrow["traffic"] > 0
    assert "p" not in heathrow, "probabilities live in the binary, not the index"


def test_pairs_reference_known_airports(bundle) -> None:
    out, _ = bundle
    airports = {item["icao"] for item in json.loads((out / "data" / "airports.json").read_text())}
    pairs = json.loads((out / "data" / "pairs.json").read_text(encoding="utf-8"))
    for pair in pairs:
        assert pair["d"] in airports and pair["a"] in airports


def test_polygons_are_usable_rings(bundle) -> None:
    out, _ = bundle
    polygons = json.loads((out / "data" / "polygons.json").read_text(encoding="utf-8"))
    assert polygons
    for polygon in polygons:
        assert len(polygon["pts"]) >= 4, "a closed ring needs at least four points"
        assert polygon["pts"][0] == polygon["pts"][-1], "rings must be closed for canvas fills"


def test_quantise_bounds() -> None:
    assert quantise(0.0) == 0
    assert quantise(1.0) == 255
    assert quantise(-0.5) == 0
    assert quantise(1.5) == 255
    assert quantise(0.5) == 128


def test_export_without_history_is_refused(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.sqlite3")
    database.initialize()
    with pytest.raises(RuntimeError):
        build_bundle(database, out_dir=tmp_path / "dist", reference=_reference())
    database.close()
