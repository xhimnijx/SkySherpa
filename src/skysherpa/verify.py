"""Static validation of a published bundle.

The browser is a harsh place to discover that ``fir_days.bin`` is one byte short: the page
shows "could not load the forecast" and the operator has to open a console to find out why.
Every one of those failures is detectable by arithmetic on the files alone.

This module exists because a publish bug already shipped once -- ``positions.json`` was
referenced by the client before it was written -- and because the bundle is a contract
between Python and JavaScript that neither language checks on its own.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = ["verify_bundle"]

HOURS = 168
GRID_W, GRID_H = 360, 180
ASSETS = ("index.html", "app.js", "core.js", "style.css")
JSON_FILES = ("firs", "airports", "pairs", "aircraft", "polygons", "positions")


def _load(path: Path, problems: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(f"missing {path.name}")
    except json.JSONDecodeError as exc:
        problems.append(f"{path.name} is not valid JSON: {exc}")
    return None


def _expect(problems: list[str], name: str, got: int, want: int) -> None:
    if got != want:
        problems.append(f"{name}: {got} bytes/entries, expected {want}")


def verify_bundle(out_dir: Path) -> list[str]:
    """Return a list of problems; an empty list means the bundle is coherent."""
    problems: list[str] = []
    if not out_dir.is_dir():
        return [f"{out_dir} does not exist"]
    data = out_dir / "data"
    if not data.is_dir():
        return [f"{data} does not exist"]

    for asset in ASSETS:
        if not (out_dir / asset).is_file():
            problems.append(f"missing web asset {asset}")

    meta = _load(data / "meta.json", problems)
    if not isinstance(meta, dict):
        return problems or ["meta.json is not an object"]

    fir_count = meta.get("fir_count")
    airport_count = meta.get("airport_count")
    pair_count = meta.get("pair_count")
    for key, value in (
        ("fir_count", fir_count),
        ("airport_count", airport_count),
        ("pair_count", pair_count),
        ("days_observed", meta.get("days_observed")),
    ):
        if not isinstance(value, int) or value < 0:
            problems.append(f"meta.{key} is not a non-negative integer: {value!r}")
            return problems

    payloads = {}
    for name in JSON_FILES:
        payload = _load(data / f"{name}.json", problems)
        if payload is None:
            continue
        if not isinstance(payload, list):
            problems.append(f"{name}.json is not a list")
            continue
        payloads[name] = payload

    if "firs" in payloads:
        _expect(problems, "firs.json", len(payloads["firs"]), fir_count)
    if "airports" in payloads:
        _expect(problems, "airports.json", len(payloads["airports"]), airport_count)
    if "pairs" in payloads:
        _expect(problems, "pairs.json", len(payloads["pairs"]), pair_count)

    def size(name: str) -> int | None:
        path = data / name
        if not path.is_file():
            problems.append(f"missing {name}")
            return None
        return path.stat().st_size

    for name, want in (
        ("fir_ctr.bin", fir_count * HOURS),
        ("fir_fss.bin", fir_count * HOURS),
        ("fir_days.bin", fir_count),
        ("airport_layers.bin", airport_count * 4 * HOURS),
        ("airport_days.bin", airport_count),
        ("fir_grid.bin", GRID_W * GRID_H * 2),
    ):
        got = size(name)
        if got is not None:
            _expect(problems, name, got, want)

    generated = meta.get("generated_at")
    if not isinstance(generated, str):
        problems.append("meta.generated_at is missing")
    else:
        try:
            moment = datetime.fromisoformat(generated)
        except ValueError:
            problems.append(f"meta.generated_at is not ISO-8601: {generated!r}")
        else:
            if moment.tzinfo is None:
                problems.append("meta.generated_at has no timezone")
            elif moment > datetime.now(UTC) + timedelta(days=1):
                problems.append(f"meta.generated_at is in the future: {generated}")

    if not (out_dir / "data" / "meta.json").is_file():
        problems.append("bundle has no meta.json, so nothing can be timestamped")

    return problems
