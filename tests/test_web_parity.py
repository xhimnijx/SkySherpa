"""Parity between the Python dispatch maths and the browser port.

The site reimplements block-time estimation in JavaScript, because the whole point is that
planning runs on the visitor's machine. Two implementations means two chances to drift, so
the browser version is checked against the Python one on every test run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from skysherpa.aircraft import load_catalog

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "blocktime_probe.mjs"

AIRCRAFT = ["A20N", "B738", "C172", "DH8D", "A388", "CONC", "E195", "C25C"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_browser_block_time_matches_python(tmp_path: Path) -> None:
    catalog = load_catalog()
    airframes = []
    for icao in AIRCRAFT:
        aircraft = catalog.get(icao)
        if aircraft is None:
            continue
        airframes.append(
            {
                "icao": aircraft.icao,
                "fl": aircraft.cruise_fl,
                "tas": aircraft.cruise_tas_kt,
                "climb": aircraft.climb_fpm,
                "descent": aircraft.descent_fpm,
                "taxi": aircraft.taxi_min,
                "range_nm": aircraft.range_nm,
            }
        )

    payload = tmp_path / "aircraft.json"
    payload.write_text(json.dumps(airframes), encoding="utf-8")

    completed = subprocess.run(
        [NODE, str(PROBE), str(payload)],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(ROOT),
    )
    data = json.loads(completed.stdout)
    distances = data["distances"]

    for airframe in airframes:
        aircraft = catalog.get(airframe["icao"])
        assert aircraft is not None
        python_times = [aircraft.estimate_block_minutes(nm) for nm in distances]
        javascript_times = data["block_times"][airframe["icao"]]
        for nm, py, js in zip(distances, python_times, javascript_times, strict=False):
            assert abs(py - js) <= 1, f"{airframe['icao']} at {nm} nm: python {py} vs js {js}"


def test_core_module_has_no_browser_dependencies() -> None:
    """core.js must stay importable in Node, or the parity check silently stops running."""
    source = (ROOT / "web" / "core.js").read_text(encoding="utf-8")
    for forbidden in ("document.", "window.", "localStorage", "fetch("):
        assert forbidden not in source, f"core.js must not touch {forbidden}"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("filename", ["core.js", "app.js"])
def test_browser_module_parses(filename: str) -> None:
    """A syntax error in app.js would only show up in a browser; catch it here."""
    completed = subprocess.run(
        [NODE, "--check", str(ROOT / "web" / filename)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert completed.returncode == 0, completed.stderr
