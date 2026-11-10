"""Parsers against names and routes the network actually uses.

The unit tests feed `callsign.parse` tidy examples. Real VATSIM callsigns are not tidy:
combined positions (`EGLL_TWR`), sector splits (`LON_S_CTR`), European compound centres
(`EDYY_D_CTR`), event callsigns, ATIS suffixes, and the occasional thing that parses to
nothing. Same for filed routes, which mix SIDs, airways, direct segments and level
annotations in whatever order the pilot's planner emitted.

Both parsers sit upstream of every statistic in the product, so a regression here is silent:
the coverage matrix quietly keys on the wrong unit.
"""

from __future__ import annotations

import json
from pathlib import Path

from skysherpa.callsign import parse as parse_callsign
from skysherpa.routes import TokenKind, parse_route

FIXTURE = Path(__file__).resolve().parent / "data" / "feed_sample.json"
FACILITIES = {"DEL", "GND", "TWR", "APP", "CTR", "FSS", "OBS", "UNKNOWN"}


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_every_real_controller_callsign_parses() -> None:
    payload = _payload()
    assert payload["controllers"], "fixture has no controllers"

    for entry in payload["controllers"]:
        callsign = str(entry.get("callsign") or "").strip()
        if not callsign:
            continue
        position = parse_callsign(callsign, facility_code=entry.get("facility"))
        assert position.raw == callsign
        assert position.facility in FACILITIES
        assert position.key, "every usable callsign must produce a staffing key"
        # An observer is a facility-0 session; anything else is real control.
        assert position.is_observer == (int(entry.get("facility") or 0) == 0)


def test_real_callsigns_mostly_resolve_to_a_known_facility() -> None:
    payload = _payload()
    positions = [
        parse_callsign(entry["callsign"], facility_code=entry.get("facility"))
        for entry in payload["controllers"]
        if str(entry.get("callsign") or "").strip()
    ]
    known = [p for p in positions if p.facility != "UNKNOWN"]
    # The feed's facility code carries the answer even when the suffix is unexpected, so
    # this should be essentially all of them.
    assert len(known) >= 0.9 * len(positions)


def test_every_real_filed_route_parses() -> None:
    payload = _payload()
    routes = [
        plan["route"]
        for plan in (p.get("flight_plan") for p in payload["pilots"])
        if plan and plan.get("route")
    ]
    assert routes, "fixture has no filed routes"

    for raw in routes:
        parsed = parse_route(raw)
        assert parsed.raw == raw
        assert parsed.tokens, f"no tokens parsed from {raw!r}"
        assert parsed.normalized
        for token in parsed.tokens:
            assert token.kind in set(TokenKind)


def test_real_routes_split_levels_from_geometry_exactly() -> None:
    """`path` must drop every level annotation and keep everything else.

    Real routes carry several -- step climbs produce more than one -- so the useful
    invariant is the exact split, not a guessed ceiling on how many there are.
    """
    payload = _payload()
    checked = 0
    for plan in (p.get("flight_plan") for p in payload["pilots"]):
        if not plan or not plan.get("route"):
            continue
        parsed = parse_route(plan["route"])
        levels = [token for token in parsed.tokens if token.kind is TokenKind.LEVEL]
        assert len(parsed.path) == len(parsed.tokens) - len(levels)
        for token in parsed.tokens:
            assert token.text, f"empty token in {plan['route']!r}"
        checked += 1
    assert checked, "fixture has no filed routes to check"


def test_route_identity_is_stable_across_case_and_spacing() -> None:
    for raw in ("DCT GATBA DCT VATZI", "dct  gatba   dct vatzi"):
        assert parse_route(raw).normalized == "DCT GATBA DCT VATZI"
