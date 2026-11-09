"""SQLite persistence for the observed network.

One writer (the ingestor), many readers (CLI, models, API). WAL mode is what makes that
safe without a server, and it is the reason this project does not need Postgres
(ADR-0001).

Three rules encoded here:

1. **Sessions are written on open and updated on every poll.** A crash therefore leaves
   an `ended_at` that is already correct, rather than a session that never existed.
2. **Every timestamp is UTC ISO-8601 text.** SQLite has no native datetime; text sorts
   correctly and is readable in a DB browser at 3 a.m.
3. **Nothing writes a CID.** Callers pass pseudonyms; the schema has no CID column.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = ["Database", "SCHEMA_VERSION", "from_iso", "to_iso"]

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1

SessionRow = sqlite3.Row


def to_iso(moment: datetime) -> str:
    """UTC, second precision, trailing Z. The canonical wire format for this database."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    raw = text.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Database:
    """A thin, boring wrapper. No ORM, no magic, no async."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    # ---- connection ----------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._conn = conn
        return self._conn

    def initialize(self) -> None:
        """Apply the schema. Idempotent; safe to call on every start."""
        conn = self.connect()
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def version(self) -> int:
        return int(self.scalar("PRAGMA user_version") or 0)

    # ---- primitives ----------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        conn = self.connect()
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        conn = self.connect()
        conn.executemany(sql, rows)
        conn.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[SessionRow]:
        return list(self.connect().execute(sql, params).fetchall())

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.connect().execute(sql, params).fetchone()
        return row[0] if row else None

    # ---- snapshots and gaps -------------------------------------------

    def record_snapshot(self, row: dict[str, Any]) -> None:
        self.execute(
            """
            INSERT OR REPLACE INTO snapshot_stat (
                ts, feed_update, version, connected_clients, unique_users, pilots,
                controllers, atc_positions, observers, atis, prefiled, fetch_ms,
                parse_ms, accepted, reject_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["ts"],
                row.get("feed_update", ""),
                row.get("version", 3),
                row.get("connected_clients", 0),
                row.get("unique_users", 0),
                row.get("pilots", 0),
                row.get("controllers", 0),
                row.get("atc_positions", 0),
                row.get("observers", 0),
                row.get("atis", 0),
                row.get("prefiled", 0),
                row.get("fetch_ms"),
                row.get("parse_ms"),
                1 if row.get("accepted", True) else 0,
                row.get("reject_reason"),
            ),
        )

    def record_gap(self, started_at: datetime, ended_at: datetime, reason: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO ingest_gap (started_at, ended_at, reason) VALUES (?,?,?)",
            (to_iso(started_at), to_iso(ended_at), reason),
        )

    def last_snapshot_ts(self) -> datetime | None:
        return from_iso(self.scalar("SELECT MAX(ts) FROM snapshot_stat"))

    # ---- positions and sessions ---------------------------------------

    def upsert_position(
        self,
        position_key: str,
        facility: str,
        airport_icao: str | None,
        fir_code: str | None,
        first_seen: datetime,
        last_seen: datetime,
    ) -> None:
        self.execute(
            """
            INSERT INTO position (position_key, facility, airport_icao, fir_code,
                                  first_seen, last_seen)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(position_key) DO UPDATE SET
                last_seen   = excluded.last_seen,
                first_seen  = MIN(position.first_seen, excluded.first_seen),
                airport_icao = COALESCE(position.airport_icao, excluded.airport_icao),
                fir_code     = COALESCE(position.fir_code, excluded.fir_code)
            """,
            (
                position_key,
                facility,
                airport_icao,
                fir_code,
                to_iso(first_seen),
                to_iso(last_seen),
            ),
        )

    def start_session(
        self,
        *,
        session_key: str,
        position_key: str,
        pseudo_id: str,
        callsign: str,
        facility_code: int,
        frequency_mhz: float | None,
        rating: int,
        server: str,
        started_at: datetime,
    ) -> None:
        self.execute(
            """
            INSERT INTO controller_session (
                session_key, position_key, pseudo_id, callsign, facility_code,
                frequency_mhz, rating, server, started_at, ended_at, duration_s,
                n_samples, is_observer
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)
            """,
            (
                session_key,
                position_key,
                pseudo_id,
                callsign,
                facility_code,
                frequency_mhz,
                rating,
                server,
                to_iso(started_at),
                to_iso(started_at),
                0,
                1,
            ),
        )

    def update_session(
        self, session_key: str, *, ended_at: datetime, n_samples: int, started_at: datetime
    ) -> None:
        duration = max(0, int((ended_at - started_at).total_seconds()))
        self.execute(
            "UPDATE controller_session SET ended_at=?, duration_s=?, n_samples=? "
            "WHERE session_key=?",
            (to_iso(ended_at), duration, n_samples, session_key),
        )

    def recent_session_for_callsign(
        self, callsign: str, *, since: datetime
    ) -> sqlite3.Row | None:
        rows = self.query(
            "SELECT * FROM controller_session WHERE callsign=? AND ended_at>=? "
            "ORDER BY ended_at DESC LIMIT 1",
            (callsign, to_iso(since)),
        )
        return rows[0] if rows else None

    # ---- flights -------------------------------------------------------

    def start_flight(
        self,
        *,
        callsign: str,
        pseudo_id: str,
        departure: str,
        arrival: str,
        alternate: str,
        aircraft_short: str,
        aircraft_full: str,
        flight_rules: str,
        cruise_alt_ft: int,
        cruise_tas_kt: int,
        filed_deptime: str,
        filed_enroute_min: int | None,
        filed_route: str,
        route_hash: str,
        first_seen: datetime,
        max_gs_kt: int,
    ) -> int:
        cursor = self.execute(
            """
            INSERT INTO flight (
                callsign, pseudo_id, dep_icao, arr_icao, alternate_icao,
                aircraft_short, aircraft_full, flight_rules, cruise_alt_ft,
                cruise_tas_kt, filed_deptime, filed_enroute_min, filed_route,
                route_hash, first_seen, last_seen, max_gs_kt
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                callsign,
                pseudo_id,
                departure,
                arrival,
                alternate,
                aircraft_short,
                aircraft_full,
                flight_rules,
                cruise_alt_ft,
                cruise_tas_kt,
                filed_deptime,
                filed_enroute_min,
                filed_route,
                route_hash,
                to_iso(first_seen),
                to_iso(first_seen),
                max_gs_kt,
            ),
        )
        return int(cursor.lastrowid or 0)

    def update_flight(
        self,
        flight_id: int,
        *,
        last_seen: datetime,
        max_gs_kt: int,
        off_block: datetime | None,
    ) -> None:
        self.execute(
            "UPDATE flight SET last_seen=?, max_gs_kt=MAX(max_gs_kt, ?), "
            "off_block=COALESCE(off_block, ?) WHERE flight_id=?",
            (to_iso(last_seen), max_gs_kt, to_iso(off_block) if off_block else None, flight_id),
        )

    def finish_flight(
        self,
        flight_id: int,
        *,
        last_seen: datetime,
        off_block: datetime | None,
        on_block: datetime | None,
        n_track_points: int,
    ) -> None:
        block_min = None
        if off_block and on_block and on_block >= off_block:
            block_min = int((on_block - off_block).total_seconds() // 60)
        self.execute(
            "UPDATE flight SET last_seen=?, off_block=COALESCE(off_block, ?), "
            "on_block=?, block_min=?, n_track_points=? WHERE flight_id=?",
            (
                to_iso(last_seen),
                to_iso(off_block) if off_block else None,
                to_iso(on_block) if on_block else None,
                block_min,
                n_track_points,
                flight_id,
            ),
        )

    def append_track(
        self,
        flight_id: int,
        points: Sequence[tuple[datetime, float, float, int | None, int | None, int | None]],
    ) -> None:
        if not points:
            return
        self.execute("DELETE FROM flight_track WHERE flight_id=?", (flight_id,))
        self.executemany(
            "INSERT INTO flight_track (flight_id, seq, ts, lat, lon, alt_ft, gs_kt, hdg) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (flight_id, index, to_iso(ts), lat, lon, alt, gs, hdg)
                for index, (ts, lat, lon, alt, gs, hdg) in enumerate(points)
            ],
        )

    def open_flights(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM flight WHERE on_block IS NULL")

    def close_orphan_flights(self, *, before: datetime) -> int:
        """Flights that were in progress when the process died.

        Their last_seen is honest (it is updated every poll), so we can close them at
        that timestamp. The trajectory buffer is lost, which is why the ingest loop
        flushes it periodically elsewhere.
        """
        rows = self.query(
            "SELECT flight_id, last_seen, off_block FROM flight "
            "WHERE on_block IS NULL AND last_seen < ?",
            (to_iso(before),),
        )
        for row in rows:
            last_seen = from_iso(row["last_seen"])
            off_block = from_iso(row["off_block"])
            self.finish_flight(
                int(row["flight_id"]),
                last_seen=last_seen or before,
                off_block=off_block,
                on_block=last_seen,
                n_track_points=int(self.scalar(
                    "SELECT COUNT(*) FROM flight_track WHERE flight_id=?",
                    (row["flight_id"],),
                ) or 0),
            )
        return len(rows)

    # ---- reporting -----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        first = from_iso(self.scalar("SELECT MIN(ts) FROM snapshot_stat"))
        last = from_iso(self.scalar("SELECT MAX(ts) FROM snapshot_stat"))
        span_h = (last - first).total_seconds() / 3600 if first and last else 0.0
        return {
            "path": str(self.path),
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
            "schema_version": self.version,
            "snapshots": int(self.scalar("SELECT COUNT(*) FROM snapshot_stat") or 0),
            "first_poll": first,
            "last_poll": last,
            "span_hours": span_h,
            "positions": int(self.scalar("SELECT COUNT(*) FROM position") or 0),
            "sessions": int(self.scalar("SELECT COUNT(*) FROM controller_session") or 0),
            "atc_hours": float(
                (self.scalar("SELECT SUM(duration_s) FROM controller_session") or 0) / 3600.0
            ),
            "flights": int(self.scalar("SELECT COUNT(*) FROM flight") or 0),
            "block_min_avg": float(self.scalar("SELECT AVG(block_min) FROM flight") or 0.0),
            "track_points": int(self.scalar("SELECT COUNT(*) FROM flight_track") or 0),
            "gaps": int(self.scalar("SELECT COUNT(*) FROM ingest_gap") or 0),
        }
