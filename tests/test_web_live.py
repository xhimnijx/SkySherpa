"""Live anchoring: the forecast blended with who is actually online.

Fetched from the browser in production; exercised here with synthetic live data so the
blend and its decay are pinned down. The properties that matter are directional: a live
centre must raise its FIR, a live tower must raise the layers it covers and not the ones
it does not, and the effect must be gone by the time the flight is well clear.

The bundle is built here rather than read from `dist/`, so these run in CI, where nothing
has been published.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skysherpa.export import build_bundle
from skysherpa.rollup import rebuild_all
from skysherpa.simulate import simulate_network
from skysherpa.store import Database
from test_export import _reference

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "live_probe.mjs"


@pytest.fixture(scope="module")
def bundle_dir(tmp_path_factory) -> Path:
    """A small but real bundle: EGTT and EGLL must exist for the probe's assertions."""
    root = tmp_path_factory.mktemp("live-bundle")
    database = Database(root / "live.sqlite3")
    database.initialize()
    end = datetime(2026, 11, 9, 22, tzinfo=UTC)
    simulate_network(database, days=21, seed=31, end=end)
    rebuild_all(database)
    out = root / "dist"
    build_bundle(database, out_dir=out, reference=_reference(), write_web_assets=False)
    database.close()
    return out / "data"


@pytest.fixture(scope="module")
def probe(bundle_dir: Path) -> dict:
    if NODE is None:
        pytest.skip("node is not installed")
    completed = subprocess.run(
        [NODE, str(PROBE), str(bundle_dir)],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(ROOT),
    )
    return json.loads(completed.stdout)


def test_live_centre_raises_its_fir(probe) -> None:
    assert probe["has_live"] is True
    assert probe["fir_after"] > probe["fir_before"]
    assert probe["fir_after"] >= 0.9


def test_live_effect_decays_to_nothing(probe) -> None:
    """Three hours out the blend must return exactly the historical probability.

    Compared against the base at *that* hour, not at the current one: the historical
    probability legitimately differs between hours, and only the live term should vanish.
    """
    assert probe["fir_live_three_hours_later"] == pytest.approx(
        probe["fir_base_three_hours_later"], abs=1e-6
    )
    assert probe["fir_live_three_hours_later"] < 0.5


def test_tower_lifts_the_layers_it_covers(probe) -> None:
    assert probe["tower_after"] >= 0.9
    assert probe["tower_after"] > probe["tower_before"]


def test_tower_does_not_lift_approach(probe) -> None:
    """A tower controller does not provide approach service; the blend must not pretend."""
    assert probe["approach_after"] == pytest.approx(probe["approach_before"], abs=1e-6)
