"""Preference learning, checked through the module the browser actually runs.

The claim being tested is narrow and testable: if you consistently pick the flight with
better ATC over the one that fits your window, the ATC weight must go up and the duration
weight must come down -- but only as far as ten clicks should move it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "taste_probe.mjs"


@pytest.fixture(scope="module")
def probe() -> dict:
    if NODE is None:
        pytest.skip("node is not installed")
    completed = subprocess.run(
        [NODE, str(PROBE)], capture_output=True, text=True, check=True, cwd=str(ROOT)
    )
    return json.loads(completed.stdout)


def test_no_choices_returns_the_preset(probe) -> None:
    for key, value in probe["no_choices"].items():
        assert value == pytest.approx(probe["prior"][key], abs=1e-6)


def test_consistently_choosing_coverage_raises_the_atc_weight(probe) -> None:
    assert probe["atc_lover"]["atc"] > probe["prior"]["atc"]
    assert probe["atc_lover"]["duration"] < probe["prior"]["duration"]


def test_consistently_choosing_duration_raises_the_duration_weight(probe) -> None:
    assert probe["time_lover"]["duration"] > probe["prior"]["duration"]
    assert probe["time_lover"]["atc"] < probe["prior"]["atc"]


def test_weights_stay_normalised_and_non_negative(probe) -> None:
    for total in probe["sums"]:
        assert total == pytest.approx(1.0, abs=1e-6)
    assert probe["non_negative"]


def test_one_choice_moves_the_weights_only_slightly(probe) -> None:
    """Regularisation is the point: ten clicks should not produce a fanatic."""
    moved = abs(probe["one_choice"]["atc"] - probe["prior"]["atc"])
    assert 0.0 < moved < 0.15
