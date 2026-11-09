"""Route tokeniser tests, using a real filed route from the live feed."""

from __future__ import annotations

import pytest

from skysherpa.routes import TokenKind, classify, parse_route

# Filed FQMA -> LPPT by a B77L at 21:49Z on 2026-11-09. Note the mid-route level
# changes embedded as `FIX/LEVEL` tokens, which is how real plans express step climbs.
REAL_ROUTE = (
    "UVDUS1A ATUSI UB529 VMV/N0494F340 UB529 GADBA UG652 VLS UM215 LUB UA607 "
    "BUMEL/N0490F360 UA607 DIR UB730 RAKOM/N0481F380 UB730 MNA UM999 ARIAM "
    "DCT BERUM DCT MGA DCT MAR DCT SVL/N0475F390 DCT PIMUR DCT ONUBA DCT NAPES "
    "DCT OSLEP DCT MINTA/N0477F400 DCT VATZI"
)


@pytest.mark.parametrize(
    ("token", "kind"),
    [
        ("DCT", TokenKind.DIRECT),
        ("N0490F360", TokenKind.LEVEL),
        ("FL350", TokenKind.LEVEL),
        ("UN857", TokenKind.AIRWAY),
        ("L612", TokenKind.AIRWAY),
        ("N45", TokenKind.AIRWAY),
        ("Q812", TokenKind.AIRWAY),
        ("BPK7F", TokenKind.PROCEDURE),
        ("UVDUS1A", TokenKind.PROCEDURE),
        ("MATIX1", TokenKind.PROCEDURE),
        ("GATBA", TokenKind.FIX),
        ("VATZI", TokenKind.FIX),
        ("", TokenKind.UNKNOWN),
        ("123456789", TokenKind.UNKNOWN),
    ],
)
def test_classify(token: str, kind: TokenKind) -> None:
    assert classify(token) is kind


def test_real_route_shape() -> None:
    route = parse_route(REAL_ROUTE)
    assert route.n_procedures == 1          # the FQMA departure
    assert route.has_direct
    assert route.n_airways >= 8
    assert route.n_fixes >= 10
    assert not route.is_empty


def test_level_annotations_are_stripped_from_identity() -> None:
    route = parse_route(REAL_ROUTE)
    assert "VMV" in route.normalized
    assert "N0494F340" not in route.normalized
    # The fix before and after the level change survive.
    assert route.normalized.split().count("UB529") == 2


def test_levels_are_parsed_to_feet() -> None:
    route = parse_route(REAL_ROUTE)
    assert route.levels_ft == [34000, 36000, 38000, 39000, 40000]


def test_route_hash_is_stable_and_level_independent() -> None:
    a = parse_route("GATBA DCT VATZI")
    b = parse_route("gatba  dct   vatzi")
    c = parse_route("GATBA/N0480F360 DCT VATZI")
    assert a.route_hash == b.route_hash == c.route_hash
    assert len(a.route_hash) == 16


def test_different_geometry_different_hash() -> None:
    assert parse_route("GATBA DCT VATZI").route_hash != parse_route("GATBA VATZI").route_hash


def test_empty_route() -> None:
    route = parse_route("")
    assert route.is_empty
    assert route.normalized == ""
    assert route.levels_ft == []


def test_slash_and_comma_separators() -> None:
    route = parse_route("EGLL, BPK7F, BPK, DCT")
    assert [t.text for t in route.path] == ["EGLL", "BPK7F", "BPK", "DCT"]
