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
    ctr = (data / "fir_ctr.bin").read_bytes()
    fss = (data / "fir_fss.bin").read_bytes()
    layers = (data / "airport_layers.bin").read_bytes()
    assert len(ctr) == meta["fir_count"] * 168
    assert len(fss) == meta["fir_count"] * 168
    assert len(layers) == meta["airport_count"] * 168 * 4
    assert max(ctr) <= 255 and max(fss) <= 255 and max(layers) <= 255


def test_meta_declares_the_layer_stack(bundle) -> None:
    _, meta = bundle
    assert meta["layers"] == ["del", "gnd", "twr", "app"]
    assert meta["airport_layer_bytes"] == 4 * 168


def test_layers_are_nested(bundle) -> None:
    """Delivery >= ground >= tower >= approach.

    A centre working top-down provides every layer beneath it, so it is impossible to be
    more likely to get a delivery clearance than a tower service. If this invariant ever
    breaks, the union logic in the exporter is wrong.
    """
    out, meta = bundle
    payload = (out / "data" / "airport_layers.bin").read_bytes()
    airports = meta["airport_count"]
    for airport in range(airports):
        base = airport * 4 * 168
        delivery, ground, tower, approach = (
            payload[base + layer * 168 : base + (layer + 1) * 168] for layer in range(4)
        )
        for hour in range(168):
            assert delivery[hour] >= ground[hour] >= tower[hour] >= approach[hour]


def test_airports_have_no_inline_probabilities(bundle) -> None:
    out, _ = bundle
    airports = json.loads((out / "data" / "airports.json").read_text(encoding="utf-8"))
    assert airports
    assert all("p" not in airport for airport in airports)


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


def test_airports_carry_expected_metadata(bundle) -> None:
    out, _ = bundle
    airports = json.loads((out / "data" / "airports.json").read_text(encoding="utf-8"))
    heathrow = next(item for item in airports if item["icao"] == "EGLL")
    assert heathrow["lat"] == pytest.approx(51.4775)
    assert heathrow["traffic"] > 0


def test_airports_declare_the_layers_they_ever_staff(bundle) -> None:
    out, _ = bundle
    airports = json.loads((out / "data" / "airports.json").read_text(encoding="utf-8"))
    assert airports
    for airport in airports:
        assert isinstance(airport["stack"], int)
        assert 0 <= airport["stack"] <= 0b1111


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
