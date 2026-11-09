"""Fleet persistence: add, replace, remove, and survive a round trip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skysherpa.aircraft import load_catalog
from skysherpa.fleet import Fleet


@pytest.fixture()
def catalog():
    return load_catalog()


@pytest.fixture()
def fleet(tmp_path: Path) -> Fleet:
    return Fleet(tmp_path / "fleet.json")


def test_empty_fleet(fleet: Fleet) -> None:
    assert fleet.load() == []
    assert len(fleet) == 0


def test_add_and_reload(fleet: Fleet, catalog) -> None:
    fleet.add("A320", catalog, variant_id="fenix-a320")
    entries = fleet.load()
    assert len(entries) == 1
    assert entries[0].icao == "A320"
    assert entries[0].variant_id == "fenix-a320"
    assert entries[0].label == "Airbus A320"


def test_add_is_case_insensitive(fleet: Fleet, catalog) -> None:
    fleet.add("a20n", catalog)
    assert fleet.load()[0].icao == "A20N"


def test_adding_same_type_replaces(fleet: Fleet, catalog) -> None:
    fleet.add("A320", catalog, variant_id="fenix-a320")
    fleet.add("A320", catalog, variant_id="toliss-a320")
    entries = fleet.load()
    assert len(entries) == 1
    assert entries[0].variant_id == "toliss-a320"


def test_default_variant_is_first_listed(fleet: Fleet, catalog) -> None:
    entry = fleet.add("B738", catalog)
    assert entry.variant_id in {v.id for v in catalog.variants_for("B738")}


def test_unknown_type_rejected(fleet: Fleet, catalog) -> None:
    with pytest.raises(KeyError):
        fleet.add("ZZZZ", catalog)


def test_unknown_variant_rejected(fleet: Fleet, catalog) -> None:
    with pytest.raises(KeyError):
        fleet.add("A20N", catalog, variant_id="not-a-real-addon")


def test_variant_from_wrong_type_rejected(fleet: Fleet, catalog) -> None:
    with pytest.raises(ValueError):
        fleet.add("B738", catalog, variant_id="fenix-a320")


def test_remove(fleet: Fleet, catalog) -> None:
    fleet.add("A20N", catalog)
    fleet.add("B738", catalog)
    assert fleet.remove("a20n") is True
    assert [e.icao for e in fleet.load()] == ["B738"]
    assert fleet.remove("A20N") is False


def test_clear(fleet: Fleet, catalog) -> None:
    fleet.add("A20N", catalog)
    fleet.clear()
    assert fleet.load() == []


def test_file_is_readable_json(fleet: Fleet, catalog) -> None:
    fleet.add("A320", catalog, variant_id="fenix-a320")
    payload = json.loads(fleet.path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["entries"] == [
        {"icao": "A320", "variant_id": "fenix-a320", "label": "Airbus A320"}
    ]


def test_corrupt_file_degrades_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "fleet.json"
    path.write_text("{not json", encoding="utf-8")
    assert Fleet(path).load() == []


def test_missing_directory_is_created(tmp_path: Path, catalog) -> None:
    fleet = Fleet(tmp_path / "nested" / "fleet.json")
    fleet.add("C172", catalog)
    assert fleet.path.exists()
