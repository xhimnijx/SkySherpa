"""Callsign parser tests, built from callsigns actually observed in the live feed.

The corpus matters more than the assertions: every one of these appeared in a single
Monday-evening snapshot, and most of them break a naive `{ICAO}_{TYPE}` assumption.
"""

from __future__ import annotations

import pytest

from skysherpa.callsign import FacilityType, is_observer_like, parse


@pytest.mark.parametrize(
    ("callsign", "code", "facility", "observer"),
    [
        ("EGLL_TWR", 4, FacilityType.TOWER, False),
        ("EDDF_C_TWR", 4, FacilityType.TOWER, False),
        ("EDDW__TWR", 4, FacilityType.TOWER, False),
        ("EDDB_S_APP", 5, FacilityType.APPROACH, False),
        ("ESGG_E_APP", 5, FacilityType.APPROACH, False),
        ("LKPR_APP", 5, FacilityType.APPROACH, False),
        ("BNA_X_DEP", 5, FacilityType.APPROACH, False),
        ("LON_S_CTR", 6, FacilityType.ENROUTE, False),
        ("EGTT_CTR", 6, FacilityType.ENROUTE, False),
        ("SUEO_CTR", 6, FacilityType.ENROUTE, False),
        ("CZEG_FSS", 1, FacilityType.FSS, False),
        ("EBCI_GND", 3, FacilityType.GROUND, False),
        ("CLT_APP", 5, FacilityType.APPROACH, False),
        ("1808096_OBS", 0, FacilityType.OBSERVER, True),
        ("BORIS_OBS", 0, FacilityType.OBSERVER, True),
        ("BAW15", 0, FacilityType.OBSERVER, True),
        ("DLH489", 0, FacilityType.OBSERVER, True),
    ],
)
def test_observed_callsigns(
    callsign: str, code: int, facility: FacilityType, observer: bool
) -> None:
    position = parse(callsign, code)
    assert position.facility is facility
    assert position.is_observer is observer
    assert position.is_atc is not observer


def test_airport_tower_resolves_with_reference() -> None:
    position = parse("EGLL_TWR", 4, known_airports={"EGLL"})
    assert position.airport == "EGLL"
    assert position.sector is None
    assert position.key == "EGLL_TWR"


def test_sub_position_preserved_in_key() -> None:
    position = parse("EDDF_C_TWR", 4, known_airports={"EDDF"})
    assert position.sector == "C"
    assert position.airport == "EDDF"
    assert position.key == "EDDF_C_TWR"


def test_double_underscore_is_normalised() -> None:
    position = parse("EDDW__TWR", 4, known_airports={"EDDW"})
    assert position.airport == "EDDW"
    assert position.key == "EDDW_TWR"


def test_fir_stem_is_not_an_airport_when_reference_says_so() -> None:
    position = parse("EGTT_CTR", 6, known_airports={"EGLL"})
    assert position.airport is None
    assert position.key == "EGTT_CTR"


def test_four_letter_stem_is_only_a_candidate_without_reference() -> None:
    position = parse("EGTT_CTR", 6)
    assert position.airport == "EGTT"  # candidate only; resolve before trusting


def test_sector_parsed_for_fir_prefix() -> None:
    position = parse("LON_S_CTR", 6, known_airports=set())
    assert position.sector == "S"
    assert position.stem == "LON_S"
    assert position.airport is None


def test_observer_suffix_beats_absent_facility_code() -> None:
    position = parse("CD_OBS")
    assert position.is_observer
    assert not position.is_atc


def test_airline_callsign_without_code_defaults_to_observer() -> None:
    assert parse("AAL2055A").is_observer
    assert parse("BER3562").is_observer


def test_atis_is_not_atc() -> None:
    position = parse("EGLL_ATIS")
    assert position.facility is FacilityType.ATIS
    assert not position.is_atc


def test_empty_callsign() -> None:
    position = parse("")
    assert position.key == ""
    assert position.is_observer


def test_is_observer_like_hot_path() -> None:
    assert is_observer_like("BAW15", 0)
    assert is_observer_like("AAL2055A")
    assert is_observer_like("1808096_OBS", 0)
    assert not is_observer_like("EGLL_TWR", 4)


# ------------------------------------------------------- airports from real stems


def test_a_position_index_does_not_hide_the_airport() -> None:
    """OTHH_1_GND is a second ground position *at* OTHH and must attribute to it.

    Observed on the live network. The trailing index defeated the four-letter test, so the
    airport was lost and that controller's coverage never reached the airport stack.
    """
    position = parse("OTHH_1_GND", facility_code=3, known_airports={"OTHH"})
    assert position.airport == "OTHH"
    assert position.key == "OTHH_1_GND", "the staffing unit is still the numbered position"


def test_us_three_letter_stems_resolve_to_k_prefixed_airports() -> None:
    """OKC_GND is at KOKC. Observed on the live network."""
    position = parse("OKC_GND", facility_code=3, known_airports={"KOKC", "EGLL"})
    assert position.airport == "KOKC"
    assert position.key == "OKC_GND"


def test_a_three_letter_stem_is_not_guessed_when_the_prefixed_airport_is_unknown() -> None:
    assert parse("OKC_GND", facility_code=3, known_airports={"EGLL"}).airport is None
    assert parse("OKC_GND", facility_code=3).airport is None


def test_a_sector_suffix_still_wins_over_a_position_index() -> None:
    position = parse("ENBR_W_APP", facility_code=5, known_airports={"ENBR"})
    assert position.sector == "W"
    assert position.airport == "ENBR"
