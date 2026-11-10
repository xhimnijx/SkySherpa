"""The parser against a real captured snapshot, not a hand-written payload.

Real VATSIM output carries quirks no synthetic fixture has: altitude as a number or a string,
sentinel frequencies, controllers with empty callsigns, pilots on the ground, timestamps with
seven fractional digits. The fixture was captured once and scrubbed (ADR-0003: no real CID
and no member name is ever stored), so these tests run offline and forever.
"""

from __future__ import annotations

import json
from pathlib import Path

from skysherpa.feed import parse_snapshot

FIXTURE = Path(__file__).resolve().parent / "data" / "feed_sample.json"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_fixture_carries_no_identities() -> None:
    """If the capture script is ever re-run, this fails before a real name is committed."""
    payload = _payload()
    for section in ("pilots", "controllers", "atis", "prefiles"):
        for entry in payload[section]:
            assert "name" not in entry, f"{section} still carries a member name"
            assert entry["cid"] < 10_000, f"{section} still carries a real CID"
    assert payload["servers"] == []


def test_a_real_snapshot_parses() -> None:
    payload = _payload()
    snapshot = parse_snapshot(payload)
    assert len(snapshot.pilots) == len(payload["pilots"])
    assert len(snapshot.controllers) == len(payload["controllers"])
    assert snapshot.prefiled == len(payload["prefiles"])


def test_real_pilots_keep_their_positions_and_plans() -> None:
    snapshot = parse_snapshot(_payload())
    positioned = [pilot for pilot in snapshot.pilots if pilot.has_position]
    assert positioned, "a live network sample always has airborne traffic"
    for pilot in positioned:
        assert pilot.latitude is not None and pilot.longitude is not None
        assert -90 <= pilot.latitude <= 90
        assert -180 <= pilot.longitude <= 180

    planned = snapshot.pilots_with_plan
    assert planned, "a live network sample always has filed flight plans"
    for pilot in planned:
        plan = pilot.flight_plan
        assert plan is not None
        assert 3 <= len(plan.departure) <= 4
        assert 3 <= len(plan.arrival) <= 4


def test_a_snapshot_without_a_timestamp_has_no_age() -> None:
    """The capture drops update_timestamp; age must be unknown rather than an error."""
    snapshot = parse_snapshot(_payload())
    assert snapshot.age_seconds() is None
