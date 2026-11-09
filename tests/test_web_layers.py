"""Layer weighting and preset behaviour, checked through the browser module itself.

The site's whole claim is that a tower-only field and a full stack are different
experiences, and that a GA preset should not punish a grass strip for having no delivery
position. Both are decided in JavaScript, so they are tested there rather than trusted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "layers_probe.mjs"


@pytest.fixture(scope="module")
def scores() -> dict[str, float]:
    if NODE is None:
        pytest.skip("node is not installed")
    completed = subprocess.run(
        [NODE, str(PROBE)], capture_output=True, text=True, check=True, cwd=str(ROOT)
    )
    return json.loads(completed.stdout)


def test_full_stack_scores_one(scores) -> None:
    assert scores["airline_full"] == pytest.approx(1.0)
    assert scores["arrival_full"] == pytest.approx(1.0)


def test_tower_only_is_penalised_for_airline_ops(scores) -> None:
    # Tower and approach carry 0.35 each on departure, so a tower alone is 0.35.
    assert scores["airline_tower_only"] == pytest.approx(0.35)


def test_ga_renormalises_over_available_layers(scores) -> None:
    # A tower-only field under the GA preset: what exists is staffed, so it scores 1.0.
    assert scores["ga_tower_only_renormalised"] == pytest.approx(1.0)
    assert scores["ga_tower_only_raw"] == pytest.approx(0.5)


def test_preset_weights_are_normalised(scores) -> None:
    assert scores["weights_sum"] == pytest.approx(1.0)
    assert scores["departure_weights_sum"] == pytest.approx(1.0)
    assert scores["arrival_weights_sum"] == pytest.approx(1.0)


def test_stack_description(scores) -> None:
    assert scores["stack_text"] == "departure: delivery + ground + tower"
    assert scores["empty_stack_text"] == "arrival: no ATC"
