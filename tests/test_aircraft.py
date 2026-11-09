"""Catalogue integrity and block-time behaviour.

The data itself is the product here: if a range is wrong, the dispatcher offers a
flight the aircraft cannot make. So the tests are mostly about the dataset, plus the
handful of properties the block-time model must satisfy.
"""

from __future__ import annotations

import pytest

from skysherpa.aircraft import CLASSES, load_catalog


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def test_catalogue_is_large(catalog) -> None:
    assert len(catalog.types) >= 100
    assert len(catalog.variants) >= 100


def test_no_validation_problems(catalog) -> None:
    problems = catalog.validate()
    assert problems == [], "\n".join(problems)


def test_all_classes_are_represented(catalog) -> None:
    present = {t.aircraft_class for t in catalog.types.values()}
    assert present == CLASSES


@pytest.mark.parametrize(
    "icao",
    ["A20N", "A320", "A321", "A359", "B738", "B38M", "B77W", "B78X", "E195", "CRJ9",
     "AT76", "DH8D", "C172", "SR22", "B350", "C25C", "A388", "CONC", "EC45", "C130"],
)
def test_key_types_present(catalog, icao: str) -> None:
    assert catalog.get(icao) is not None, icao


def test_lookup_is_case_insensitive(catalog) -> None:
    aircraft = catalog.get("a20n")
    assert aircraft is not None
    assert aircraft.icao == "A20N"


def test_unknown_type_is_none(catalog) -> None:
    assert catalog.get("ZZZZ") is None


def test_every_type_has_a_selectable_variant(catalog) -> None:
    for icao in catalog.types:
        assert catalog.variants_for(icao), f"{icao} has no variant"


def test_variants_reference_known_types(catalog) -> None:
    unknown = {v.icao for v in catalog.variants if v.icao not in catalog.types}
    assert unknown == set()


def test_variant_mapping_matches_the_real_world(catalog) -> None:
    # Fenix models the A320ceo; FlyByWire models the A320neo. Getting this wrong
    # would silently change the performance behind a user's favourite aircraft.
    assert catalog.variant("fenix-a320").icao == "A320"
    assert catalog.variant("fbw-a32nx").icao == "A20N"
    assert catalog.variant("zibo-b738").icao == "B738"


def test_variant_ids_are_unique(catalog) -> None:
    ids = [v.id for v in catalog.variants]
    assert len(ids) == len(set(ids))


def test_sim_filter_is_consistent(catalog) -> None:
    msfs = catalog.filter(sim="msfs")
    assert msfs
    for aircraft in msfs:
        sims = {v.sim for v in catalog.variants_for(aircraft.icao)}
        assert sims & {"msfs", "any"}, aircraft.icao


def test_freeware_filter_finds_community_favourites(catalog) -> None:
    freeware = catalog.filter(freeware=True)
    ids = {v.id for aircraft in freeware for v in catalog.variants_for(aircraft.icao)}
    assert {"fbw-a32nx", "zibo-b738", "fbw-a388"} <= ids


def test_search(catalog) -> None:
    assert any(t.icao == "B738" for t in catalog.search("737-800"))
    assert any(t.icao == "A20N" for t in catalog.search("airbus a320neo"))
    assert catalog.search("")  # empty search returns everything


def test_class_filter(catalog) -> None:
    general = catalog.filter(aircraft_class="general")
    assert general
    assert all(t.aircraft_class == "general" for t in general)


def test_runway_filter(catalog) -> None:
    short_field = catalog.filter(max_runway_ft=3000)
    assert short_field
    assert all(0 < t.min_runway_ft <= 3000 for t in short_field)


def test_helicopters_have_no_runway_requirement(catalog) -> None:
    for aircraft in catalog.types.values():
        if aircraft.aircraft_class == "helicopter":
            assert aircraft.min_runway_ft == 0


def test_widebodies_are_heavy(catalog) -> None:
    for icao in ("A388", "B77W", "B748", "A359"):
        aircraft = catalog.get(icao)
        assert aircraft is not None
        assert aircraft.wake in {"H", "J"}
        assert aircraft.mtow_kg > 200_000


# ---------------------------------------------------------------- block times


def test_block_time_is_monotonic(catalog) -> None:
    aircraft = catalog.get("A20N")
    assert aircraft is not None
    times = [aircraft.estimate_block_minutes(d) for d in (50, 100, 300, 600, 1200, 2400)]
    assert times == sorted(times)
    assert len(set(times)) == len(times)


def test_block_time_short_haul_is_plausible(catalog) -> None:
    aircraft = catalog.get("A20N")
    assert aircraft is not None
    assert 90 <= aircraft.estimate_block_minutes(600) <= 120


def test_block_time_medium_haul_is_plausible(catalog) -> None:
    aircraft = catalog.get("B738")
    assert aircraft is not None
    assert 140 <= aircraft.estimate_block_minutes(1000) <= 180


def test_block_time_ga_is_plausible(catalog) -> None:
    aircraft = catalog.get("C172")
    assert aircraft is not None
    assert 45 <= aircraft.estimate_block_minutes(100) <= 75


def test_block_time_turboprop_is_plausible(catalog) -> None:
    aircraft = catalog.get("DH8D")
    assert aircraft is not None
    assert 60 <= aircraft.estimate_block_minutes(300) <= 85


def test_short_hop_does_not_climb_to_cruise_level(catalog) -> None:
    aircraft = catalog.get("A20N")
    assert aircraft is not None
    hop = aircraft.estimate_block_minutes(30)
    assert hop < 45
    assert hop < aircraft.estimate_block_minutes(60)


def test_zero_distance_is_ground_allowance(catalog) -> None:
    aircraft = catalog.get("A20N")
    assert aircraft is not None
    assert aircraft.estimate_block_minutes(0) <= aircraft.taxi_min + 5


def test_headwind_costs_time(catalog) -> None:
    aircraft = catalog.get("A20N")
    assert aircraft is not None
    headwind = aircraft.estimate_block_minutes(600, wind_kt=-50)
    tailwind = aircraft.estimate_block_minutes(600, wind_kt=50)
    assert headwind > tailwind


def test_slower_aircraft_takes_longer(catalog) -> None:
    jet = catalog.get("A20N")
    prop = catalog.get("DH8D")
    assert jet is not None and prop is not None
    assert prop.estimate_block_minutes(500) > jet.estimate_block_minutes(500)


def test_negative_distance_is_rejected(catalog) -> None:
    aircraft = catalog.get("A20N")
    assert aircraft is not None
    with pytest.raises(ValueError):
        aircraft.estimate_block_minutes(-1)


def test_cruise_altitude_is_clamped_to_ceiling(catalog) -> None:
    aircraft = catalog.get("C172")
    assert aircraft is not None
    absurd = aircraft.estimate_block_minutes(200, cruise_alt_ft=50_000)
    at_ceiling = aircraft.estimate_block_minutes(200, cruise_alt_ft=aircraft.ceiling_ft)
    assert absurd == at_ceiling
