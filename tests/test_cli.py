"""CLI smoke tests: the commands exist, parse, and return sane exit codes."""

from __future__ import annotations

import json
from pathlib import Path

from skysherpa.cli import main


def test_version(capsys) -> None:
    assert main(["version"]) == 0
    assert "skysherpa" in capsys.readouterr().out


def test_parse_callsign(capsys) -> None:
    assert main(["parse-callsign", "EDDF_C_TWR", "--facility-code", "4"]) == 0
    out = capsys.readouterr().out
    assert "EDDF_C_TWR" in out
    assert "facility  TWR" in out
    assert "observer  False" in out


def test_parse_callsign_observer(capsys) -> None:
    assert main(["parse-callsign", "BAW15", "--facility-code", "0"]) == 0
    out = capsys.readouterr().out
    assert "facility  OBS" in out
    assert "atc       False" in out


def test_parse_route(capsys) -> None:
    assert main(["parse-route", "GATBA DCT VATZI/N0480F360"]) == 0
    out = capsys.readouterr().out
    assert "GATBA" in out
    assert "direct" in out
    assert "FL360" in out


def test_init_creates_directories(tmp_path: Path, capsys) -> None:
    target = tmp_path / "data"
    assert main(["init", "--data-dir", str(target)]) == 0
    assert (target / "cache").is_dir()
    assert "ready" in capsys.readouterr().out


def test_status_requires_a_database(tmp_path: Path, capsys) -> None:
    assert main(["status", "--data-dir", str(tmp_path)]) == 1
    assert "no database" in capsys.readouterr().err


def test_status_reports_readiness(tmp_path: Path, capsys) -> None:
    from skysherpa.store import Database

    database = Database(tmp_path / "skysherpa.sqlite3")
    database.initialize()
    database.close()

    assert main(["status", "--data-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["days_observed"] == 0
    assert payload["city_pairs"] == 0
    names = [item["name"] for item in payload["readiness"]]
    assert names == ["planning", "forecast", "backtest", "calibration"]
    assert all(not item["ready"] for item in payload["readiness"])
    assert payload["readiness"][2]["days_remaining"] == 35


def test_status_prints_the_blocking_items(tmp_path: Path, capsys) -> None:
    from skysherpa.store import Database

    database = Database(tmp_path / "skysherpa.sqlite3")
    database.initialize()
    database.close()

    assert main(["status", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "readiness" in out
    assert "waiting on" in out


def test_status_flags_a_stalled_collector(tmp_path: Path, capsys) -> None:
    """A dead ingestor must not look like a healthy database.

    The collector is the only component that cannot fail visibly: everything else is
    downstream of it, so an empty or frozen database looks like "no traffic tonight".
    """
    from skysherpa.store import Database

    database = Database(tmp_path / "skysherpa.sqlite3")
    database.initialize()
    database.execute(
        "INSERT INTO snapshot_stat (ts, feed_update, version, connected_clients, "
        "unique_users, pilots, controllers, atc_positions, observers, atis, prefiled) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-11-09T20:00:00Z", "20261109200000", 3, 1, 1, 10, 2, 2, 0, 0, 0),
    )
    database.close()

    assert main(["status", "--data-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["collector"]["alive"] is False
    assert payload["collector"]["age_seconds"] > 300

    assert main(["status", "--data-dir", str(tmp_path)]) == 0
    assert "STALLED" in capsys.readouterr().out


def test_status_reports_no_polls(tmp_path: Path, capsys) -> None:
    from skysherpa.store import Database

    database = Database(tmp_path / "skysherpa.sqlite3")
    database.initialize()
    database.close()

    assert main(["status", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "no polls yet" in out


def _current_snapshot(database) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    database.execute(
        "INSERT INTO snapshot_stat (ts, feed_update, version, connected_clients, "
        "unique_users, pilots, controllers, atc_positions, observers, atis, prefiled) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (now, "x", 3, 1, 1, 10, 2, 2, 0, 0, 0),
    )


def test_check_is_unhappy_without_a_publisher(tmp_path: Path, capsys) -> None:
    from skysherpa.store import Database

    database = Database(tmp_path / "skysherpa.sqlite3")
    database.initialize()
    _current_snapshot(database)
    database.close()

    assert main(["status", "--data-dir", str(tmp_path), "--site", str(tmp_path), "--check"]) == 1
    assert "publisher" in capsys.readouterr().err


def test_check_is_happy_when_both_are_current(tmp_path: Path, capsys) -> None:
    from datetime import UTC, datetime

    from skysherpa.store import Database

    database = Database(tmp_path / "skysherpa.sqlite3")
    database.initialize()
    _current_snapshot(database)
    database.close()

    site = tmp_path / "dist"
    site.mkdir()
    (site / ".publisher.json").write_text(
        json.dumps(
            {
                "at": datetime.now(UTC).isoformat(),
                "ok": True,
                "interval": 600,
                "seconds": 1.2,
            }
        ),
        encoding="utf-8",
    )

    assert main(["status", "--data-dir", str(tmp_path), "--site", str(tmp_path), "--check"]) == 0
    assert "healthy" in capsys.readouterr().out


def test_check_flags_a_stalled_publisher(tmp_path: Path, capsys) -> None:
    from datetime import UTC, datetime, timedelta

    from skysherpa.store import Database

    database = Database(tmp_path / "skysherpa.sqlite3")
    database.initialize()
    _current_snapshot(database)
    database.close()

    site = tmp_path / "dist"
    site.mkdir()
    (site / ".publisher.json").write_text(
        json.dumps(
            {
                "at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                "ok": True,
                "interval": 600,
            }
        ),
        encoding="utf-8",
    )

    assert main(["status", "--data-dir", str(tmp_path), "--site", str(tmp_path), "--check"]) == 1
    assert "publisher" in capsys.readouterr().err


def test_check_flags_a_hung_cycle(tmp_path: Path, capsys) -> None:
    """A cycle that started and never finished must fail the check even if it is recent."""
    from datetime import UTC, datetime

    from skysherpa.store import Database

    database = Database(tmp_path / "skysherpa.sqlite3")
    database.initialize()
    _current_snapshot(database)
    database.close()

    site = tmp_path / "dist"
    site.mkdir()
    (site / ".publisher.json").write_text(
        json.dumps({"at": datetime.now(UTC).isoformat(), "phase": "hung", "interval": 600}),
        encoding="utf-8",
    )

    assert main(["status", "--data-dir", str(tmp_path), "--site", str(tmp_path), "--check"]) == 1
    assert "HUNG" in capsys.readouterr().err


def test_console_entry_point_takes_no_arguments(monkeypatch, capsys) -> None:
    """`[project.scripts]` calls main() with no arguments, so argv must default to sys.argv."""
    import sys

    from skysherpa.cli import main

    monkeypatch.setattr(sys, "argv", ["skysherpa", "version"])
    assert main() == 0
    assert "skysherpa" in capsys.readouterr().out


def test_unknown_command_exits_nonzero() -> None:
    try:
        main(["nonsense"])
    except SystemExit as exc:
        assert exc.code != 0
    else:  # pragma: no cover
        raise AssertionError("expected SystemExit")


def test_sniff_json_shape_without_network(monkeypatch, capsys) -> None:
    from skysherpa import cli
    from skysherpa.feed import parse_snapshot

    payload = {
        "general": {
            "version": 3,
            "update_timestamp": "2026-11-09T21:49:41.2144888Z",
            "connected_clients": 10,
            "unique_users": 9,
        },
        "pilots": [
            {
                "cid": 1,
                "callsign": "BAW1",
                "latitude": 51.0,
                "longitude": -0.5,
                "altitude": "30000",
                "groundspeed": 400,
                "flight_plan": {
                    "departure": "EGLL",
                    "arrival": "LEPA",
                    "aircraft_short": "A20N",
                    "route": "DCT GATBA",
                    "deptime": "1200",
                    "enroute_time": "0200",
                },
            }
        ],
        "controllers": [
            {"cid": 10, "callsign": "EGLL_TWR", "facility": 4, "frequency": "118.5", "rating": 4}
        ],
        "atis": [],
        "servers": [],
        "prefiles": [],
    }
    snapshot = parse_snapshot(payload)
    monkeypatch.setattr(cli, "fetch_snapshot", lambda settings: snapshot)
    assert main(["sniff", "--json", "--no-coverage"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["pilots"] == 1
    assert data["atc"] == 1
    assert data["facilities"] == {"TWR": 1}
    assert data["coverage"] is None
    assert data["routes"]["top_pairs"][0] == {"dep": "EGLL", "arr": "LEPA", "count": 1}
