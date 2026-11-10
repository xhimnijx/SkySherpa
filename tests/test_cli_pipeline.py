"""The CLI wiring of the data pipeline.

Modules are tested individually -- ingest, rollup, export, verify, prune all have their own
suites -- but the *commands* were not: argument parsing, defaults, exit codes and the
self-check that publish runs at the end of every build. Those are the parts a user touches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_export import _reference

from skysherpa import cli
from skysherpa.rollup import rebuild_all
from skysherpa.simulate import simulate_flights, simulate_network
from skysherpa.store import Database


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    """A populated database, and a reference so publish does not reach the network."""
    directory = tmp_path / "data"
    database = Database(directory / "skysherpa.sqlite3")
    database.initialize()
    end = datetime(2026, 11, 9, 22, tzinfo=UTC)
    simulate_network(database, days=36, seed=41, end=end)
    simulate_flights(database, days=36, seed=42, end=end)
    rebuild_all(database)
    database.close()
    monkeypatch.setattr(cli, "load_reference", lambda settings: _reference())
    return directory


def test_publish_builds_a_bundle_and_verifies_it(data_dir: Path, tmp_path: Path, capsys) -> None:
    out = tmp_path / "dist"
    assert cli.main(["publish", "--data-dir", str(data_dir), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "bundle written" in printed
    assert (out / "data" / "meta.json").is_file()
    assert (out / "index.html").is_file()
    # publish runs verification itself; a second pass must agree.
    assert cli.main(["verify", "--out", str(out)]) == 0
    assert "coherent" in capsys.readouterr().out


def test_verify_reports_a_broken_bundle(data_dir: Path, tmp_path: Path, capsys) -> None:
    out = tmp_path / "dist"
    assert cli.main(["publish", "--data-dir", str(data_dir), "--out", str(out)]) == 0
    (out / "data" / "fir_ctr.bin").write_bytes(b"short")
    assert cli.main(["verify", "--out", str(out)]) == 1
    assert "fir_ctr.bin" in capsys.readouterr().err


def test_status_reports_readiness(data_dir: Path, tmp_path: Path, capsys) -> None:
    assert cli.main(["status", "--data-dir", str(data_dir), "--site", str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert "readiness" in printed
    assert "backtest" in printed
    # 36 simulated days: the backtest gate is open, the forecast gate is open too.
    assert "[x] backtest" in printed


def test_backtest_runs_through_the_cli(data_dir: Path, capsys) -> None:
    assert cli.main(["backtest", "--data-dir", str(data_dir), "--folds", "2"]) == 0
    printed = capsys.readouterr().out
    assert "backing" not in printed  # sanity: no stray debug text
    assert "PASS" in printed
    assert "backed cells" in printed


def test_doctor_renders_and_reports_degradation(monkeypatch, capsys) -> None:
    """The health check a 24/7 operator runs, with its source checks stubbed out."""
    from skysherpa import doctor as doctor_module

    checks = [
        doctor_module.Check(name="feed", ok=True, detail="1,200 pilots | 60 ATC", elapsed_ms=300),
        doctor_module.Check(
            name="atc_history",
            ok=False,
            detail="TimeoutError: read timed out",
            critical=False,
            elapsed_ms=8000,
        ),
    ]
    monkeypatch.setattr(doctor_module, "run_checks", lambda settings, include_weather=True: checks)

    code = cli.main(["doctor"])
    printed = capsys.readouterr().out
    assert "feed" in printed
    assert "atc_history" in printed
    assert "degraded" in printed
    # A non-critical failure must not fail the command.
    assert code == 0


def test_doctor_fails_when_a_critical_check_fails(monkeypatch, capsys) -> None:
    from skysherpa import doctor as doctor_module

    checks = [doctor_module.Check(name="feed", ok=False, detail="403 from the feed", critical=True)]
    monkeypatch.setattr(doctor_module, "run_checks", lambda settings, include_weather=True: checks)
    assert cli.main(["doctor"]) == 1
    # A critical failure is not "degraded": it says FAILED, which is the distinction the
    # command exists to make (VATSIM is down vs. one optional source is slow).
    assert "1 FAILED" in capsys.readouterr().out


def test_db_and_prune_speak_without_touching_data(data_dir: Path, capsys) -> None:
    assert cli.main(["db", "--data-dir", str(data_dir)]) == 0
    assert "snapshots" in capsys.readouterr().out

    # keep-days beyond the history: nothing to prune, and a dry run changes nothing either way
    assert cli.main(["prune", "--data-dir", str(data_dir), "--keep-days", "3650"]) == 0
    assert "kept the last" in capsys.readouterr().out


def test_ingest_once_records_a_snapshot(tmp_path: Path, monkeypatch, capsys) -> None:
    """The command that runs 24/7, driven once with a stubbed feed."""
    from factories import controller, flight_plan, pilot, snapshot, stamp

    from skysherpa import ingest as ingest_module

    snap = snapshot(
        stamp(22, 0),
        controllers=[controller("EGLL_TWR")],
        pilots=[pilot("BAW1", plan=flight_plan("EGLL", "LEPA"))],
    )
    monkeypatch.setattr(ingest_module, "fetch_snapshot", lambda settings: snap)

    assert cli.main(["ingest", "--once", "--data-dir", str(tmp_path)]) == 0
    database = Database(tmp_path / "skysherpa.sqlite3")
    stats = database.stats()
    assert stats["snapshots"] == 1
    assert stats["sessions"] == 1
    assert stats["flights"] == 1
    database.close()


def test_simulate_and_rollup_through_the_cli(tmp_path: Path, capsys) -> None:
    assert cli.main(["simulate", "--data-dir", str(tmp_path), "--days", "5"]) == 0
    assert "synthetic network" in capsys.readouterr().out

    assert cli.main(["rollup", "--data-dir", str(tmp_path)]) == 0
    assert "rolled up" in capsys.readouterr().out


def test_rehearse_runs_a_short_rehearsal(tmp_path: Path, monkeypatch, capsys) -> None:
    """The capacity tool, at a size that finishes in seconds."""
    from skysherpa import rehearse as rehearse_module

    monkeypatch.setattr(rehearse_module, "default_reference", lambda: _reference())
    assert (
        cli.main(
            [
                "rehearse",
                "--days",
                "35",
                "--airports",
                "2",
                "--flights-per-day",
                "6",
                "--seed",
                "7",
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "rehearsal:" in printed
    assert "35 days" in printed
    assert "storage" in printed
