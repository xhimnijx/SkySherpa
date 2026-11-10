"""Bundle validation: the contract between the exporter and the browser.

Each test corrupts exactly one thing and asserts the validator names it. The point is not
that the checks exist but that they fire on the failures that actually happened: a missing
file the client fetches, and a binary whose length disagrees with the counts in meta.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from skysherpa.verify import verify_bundle

HOURS = 168


def make_bundle(
    root: Path,
    *,
    fir_count: int = 2,
    airport_count: int = 1,
    pair_count: int = 0,
) -> Path:
    out = root / "dist"
    data = out / "data"
    data.mkdir(parents=True)
    for asset in ("index.html", "app.js", "core.js", "style.css"):
        (out / asset).write_text("// asset", encoding="utf-8")

    meta = {
        "generated_at": datetime.now(UTC).isoformat(),
        "hours_per_week": HOURS,
        "fir_count": fir_count,
        "airport_count": airport_count,
        "pair_count": pair_count,
        "days_observed": 3,
        "positions_modelled": fir_count,
        "grid": {"width": 360, "height": 180},
        "layers": ["del", "gnd", "twr", "app"],
    }
    (data / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (data / "firs.json").write_text(
        json.dumps([{"code": f"F{index}", "name": "x"} for index in range(fir_count)]),
        encoding="utf-8",
    )
    (data / "airports.json").write_text(
        json.dumps([{"icao": "EGLL", "name": "London"}] * airport_count), encoding="utf-8"
    )
    for name in ("pairs", "aircraft", "polygons", "positions"):
        (data / f"{name}.json").write_text(
            json.dumps([{}] * (pair_count if name == "pairs" else 0)), encoding="utf-8"
        )
    (data / "fir_ctr.bin").write_bytes(bytes(fir_count * HOURS))
    (data / "fir_fss.bin").write_bytes(bytes(fir_count * HOURS))
    (data / "fir_days.bin").write_bytes(bytes(fir_count))
    (data / "airport_layers.bin").write_bytes(bytes(airport_count * 4 * HOURS))
    (data / "airport_days.bin").write_bytes(bytes(airport_count))
    (data / "fir_grid.bin").write_bytes(bytes(360 * 180 * 2))
    return out


def test_assets_can_be_skipped_for_a_data_only_publish(tmp_path: Path) -> None:
    """`publish --no-assets` is legitimate; verification must not fail the build over it."""
    out = make_bundle(tmp_path)
    for asset in ("index.html", "app.js", "core.js", "style.css"):
        (out / asset).unlink()
    assert verify_bundle(out) != []
    assert verify_bundle(out, check_assets=False) == []


def test_a_coherent_bundle_has_no_problems(tmp_path: Path) -> None:
    assert verify_bundle(make_bundle(tmp_path)) == []


def test_missing_data_directory_is_reported(tmp_path: Path) -> None:
    out = make_bundle(tmp_path)
    (out / "data").rename(tmp_path / "moved")
    problems = verify_bundle(out)
    assert any("data does not exist" in problem for problem in problems)


def test_truncated_binary_is_reported(tmp_path: Path) -> None:
    out = make_bundle(tmp_path)
    (out / "data" / "fir_ctr.bin").write_bytes(bytes(10))
    problems = verify_bundle(out)
    assert any("fir_ctr.bin" in problem for problem in problems)


def test_missing_file_the_client_fetches_is_reported(tmp_path: Path) -> None:
    """positions.json was once referenced by the client and never written."""
    out = make_bundle(tmp_path)
    (out / "data" / "positions.json").unlink()
    problems = verify_bundle(out)
    assert any("positions.json" in problem for problem in problems)


def test_missing_web_asset_is_reported(tmp_path: Path) -> None:
    out = make_bundle(tmp_path)
    (out / "index.html").unlink()
    problems = verify_bundle(out)
    assert any("index.html" in problem for problem in problems)


def test_count_mismatch_between_meta_and_index_is_reported(tmp_path: Path) -> None:
    out = make_bundle(tmp_path, fir_count=2)
    meta_path = out / "data" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["fir_count"] = 5
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    problems = verify_bundle(out)
    assert any("firs.json" in problem for problem in problems)
    assert any("fir_ctr.bin" in problem for problem in problems)


def test_future_timestamp_is_reported(tmp_path: Path) -> None:
    out = make_bundle(tmp_path)
    meta_path = out / "data" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["generated_at"] = "2099-01-01T00:00:00+00:00"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    problems = verify_bundle(out)
    assert any("future" in problem for problem in problems)


def test_invalid_json_is_reported(tmp_path: Path) -> None:
    out = make_bundle(tmp_path)
    (out / "data" / "pairs.json").write_text("{not json", encoding="utf-8")
    problems = verify_bundle(out)
    assert any("pairs.json" in problem for problem in problems)
