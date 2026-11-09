"""CLI tests for the aircraft catalogue and fleet manager."""

from __future__ import annotations

import json
from pathlib import Path

from skysherpa.cli import main


def test_aircraft_list(capsys) -> None:
    assert main(["aircraft", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "aircraft types" in out
    assert "ICAO" in out


def test_aircraft_detail(capsys) -> None:
    assert main(["aircraft", "A20N"]) == 0
    out = capsys.readouterr().out
    assert "Airbus A320neo" in out
    assert "FlyByWire A32NX" in out
    assert "estimated block time" in out
    assert "add-ons" in out


def test_aircraft_detail_is_case_insensitive(capsys) -> None:
    assert main(["aircraft", "b738"]) == 0
    assert "Boeing 737-800" in capsys.readouterr().out


def test_aircraft_unknown_exits_nonzero(capsys) -> None:
    assert main(["aircraft", "ZZZZ"]) == 2
    assert "unknown aircraft" in capsys.readouterr().err


def test_aircraft_class_filter_json(capsys) -> None:
    assert main(["aircraft", "--class", "helicopter", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] >= 3
    assert all(t["class"] == "helicopter" for t in payload["types"])


def test_aircraft_sim_filter_json(capsys) -> None:
    assert main(["aircraft", "--sim", "xplane", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] >= 5


def test_aircraft_search(capsys) -> None:
    assert main(["aircraft", "--search", "concorde", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [t["icao"] for t in payload["types"]] == ["CONC"]


def test_aircraft_no_matches(capsys) -> None:
    assert main(["aircraft", "--search", "zzzzzz"]) == 2


def test_fleet_empty_message(tmp_path: Path, capsys) -> None:
    assert main(["fleet", "--data-dir", str(tmp_path)]) == 0
    assert "empty" in capsys.readouterr().out


def test_fleet_add_then_list(tmp_path: Path, capsys) -> None:
    assert (
        main(
            [
                "fleet",
                "--data-dir",
                str(tmp_path),
                "--add",
                "A320",
                "--variant",
                "fenix-a320",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "added A320" in out
    assert (tmp_path / "fleet.json").exists()
    assert "Fenix A320" in out


def test_fleet_remove(tmp_path: Path, capsys) -> None:
    main(["fleet", "--data-dir", str(tmp_path), "--add", "C172"])
    capsys.readouterr()
    assert main(["fleet", "--data-dir", str(tmp_path), "--remove", "C172"]) == 0
    assert "removed C172" in capsys.readouterr().out


def test_fleet_remove_missing(tmp_path: Path, capsys) -> None:
    assert main(["fleet", "--data-dir", str(tmp_path), "--remove", "C172"]) == 2


def test_fleet_unknown_type(tmp_path: Path, capsys) -> None:
    assert main(["fleet", "--data-dir", str(tmp_path), "--add", "ZZZZ"]) == 2
    assert "unknown aircraft" in capsys.readouterr().err


def test_fleet_variant_mismatch(tmp_path: Path, capsys) -> None:
    assert (
        main(
            [
                "fleet",
                "--data-dir",
                str(tmp_path),
                "--add",
                "B738",
                "--variant",
                "fenix-a320",
            ]
        )
        == 2
    )


def test_fleet_clear(tmp_path: Path, capsys) -> None:
    main(["fleet", "--data-dir", str(tmp_path), "--add", "A20N"])
    capsys.readouterr()
    assert main(["fleet", "--data-dir", str(tmp_path), "--clear"]) == 0
    assert "cleared" in capsys.readouterr().out


def test_fleet_json(tmp_path: Path, capsys) -> None:
    main(["fleet", "--data-dir", str(tmp_path), "--add", "A20N"])
    capsys.readouterr()
    assert main(["fleet", "--data-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["entries"][0]["icao"] == "A20N"
