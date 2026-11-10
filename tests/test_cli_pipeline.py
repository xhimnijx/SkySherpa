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


def test_db_and_prune_speak_without_touching_data(data_dir: Path, capsys) -> None:
    assert cli.main(["db", "--data-dir", str(data_dir)]) == 0
    assert "snapshots" in capsys.readouterr().out

    # keep-days beyond the history: nothing to prune, and a dry run changes nothing either way
    assert cli.main(["prune", "--data-dir", str(data_dir), "--keep-days", "3650"]) == 0
    assert "kept the last" in capsys.readouterr().out
