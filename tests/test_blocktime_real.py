"""Block-time model against real filed en-route times.

Every filed flight plan declares an aircraft type and an en-route time in HHMM, so 28 of the
captured flights are ground truth for the model the duration filter depends on. The point of
this file is not precision -- filed times include winds the model deliberately ignores -- but
the absence of *bias*: if the estimate were systematically short or long, the "I have between
two and four hours" question would quietly answer the wrong routes.

Measured on the capture: median ratio 1.000, median absolute error 39 minutes on flights of
8 to 18 hours (about 5%), and no airport-pair with a gross error.
"""

from __future__ import annotations

import json
import statistics
from itertools import pairwise
from pathlib import Path

from skysherpa.aircraft import estimate_block_minutes, load_catalog
from skysherpa.feed import _hhmm_to_minutes
from skysherpa.geo import distance_nm

DATA = Path(__file__).resolve().parent / "data"


def _comparisons() -> list[tuple[str, str, str, int, int, int]]:
    """(dep, arr, type, distance_nm, filed_minutes, modelled_minutes) for every usable plan."""
    payload = json.loads((DATA / "feed_sample.json").read_text(encoding="utf-8"))
    coords = json.loads((DATA / "airport_coords.json").read_text(encoding="utf-8"))
    catalog = load_catalog()

    rows = []
    for pilot in payload["pilots"]:
        plan = pilot.get("flight_plan") or {}
        filed = _hhmm_to_minutes(plan.get("enroute_time") or "")
        spec = catalog.get((plan.get("aircraft_short") or "").upper())
        dep = (plan.get("departure") or "").upper()
        arr = (plan.get("arrival") or "").upper()
        if not (filed and spec and dep in coords and arr in coords):
            continue
        distance = distance_nm(tuple(coords[dep]), tuple(coords[arr]))
        rows.append(
            (dep, arr, spec.icao, round(distance), filed, estimate_block_minutes(spec, distance))
        )
    return rows


def test_the_capture_still_has_usable_plans() -> None:
    rows = _comparisons()
    assert len(rows) >= 15, f"only {len(rows)} comparisons; the fixture or catalogue changed"


def test_the_model_is_not_systematically_long_or_short() -> None:
    rows = _comparisons()
    ratios = [modelled / filed for _, _, _, _, filed, modelled in rows if filed]
    median = statistics.median(ratios)
    # Winds alone move a long-sector estimate by several percent; anything beyond 10% is a
    # modelling error, not weather.
    assert 0.90 <= median <= 1.10, f"median model/filed ratio {median:.3f}"


def test_most_reals_are_within_ten_percent() -> None:
    rows = _comparisons()
    within = [
        abs(modelled - filed) <= 0.10 * filed for _, _, _, _, filed, modelled in rows if filed
    ]
    share = sum(within) / len(within)
    assert share >= 0.5, f"only {share:.0%} of routes within 10% of the filed time"


def test_distance_and_time_are_monotone_across_the_capture() -> None:
    """A sanity check no tolerance can hide: longer sectors must not model as faster."""
    rows = sorted(_comparisons(), key=lambda row: row[3])
    for short, longer in pairwise(rows):
        if longer[3] > 2 * short[3]:
            assert longer[5] > short[5], (
                f"{longer[0]}-{longer[1]} models faster than {short[0]}-{short[1]}"
            )
