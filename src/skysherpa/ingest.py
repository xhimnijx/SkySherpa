"""The observer: turn a stream of 30-second snapshots into sessions and flights.

This is the piece that has to run for months without supervision, so the state machine
is deliberately boring and crash-safe:

* a controller session is **inserted when first seen and updated on every poll**, so a
  hard kill leaves an ``ended_at`` that is already correct;
* a reconnect inside the merge window extends the existing row instead of opening a
  second one, because a forty-second network blip is not a new shift;
* a poll carrying a timestamp we have already seen is a duplicate and is dropped, so a
  retry can never double-count a controller's time;
* a gap longer than a few intervals is recorded, because the coverage model has to tell
  "nobody was controlling" apart from "we were not watching".

:meth:`Ingestor.poll` is synchronous and takes an already-parsed snapshot, which keeps
the state machine testable without a network or a clock.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import Settings
from .feed import Controller, FeedError, FlightPlan, Pilot, Snapshot, fetch_snapshot
from .geo import rdp_keep_indices
from .identity import Identity
from .routes import parse_route
from .staticdata import StaticReference, load_reference
from .store import Database, from_iso, to_iso

__all__ = ["IngestConfig", "Ingestor", "PollResult", "run_ingest"]


@dataclass(frozen=True, slots=True)
class IngestConfig:
    interval_s: int = 30
    #: A gap is recorded when two polls are further apart than this multiple of the interval.
    gap_factor: float = 3.0
    #: A position reappearing within this window continues its previous session.
    reconnect_merge_s: int = 90
    #: Trajectory sampling cadence per airborne flight.
    track_sample_s: int = 60
    #: RDP tolerance applied when a flight closes (~25 points for a 3 h flight).
    track_tolerance_nm: float = 0.5


@dataclass(slots=True)
class PollResult:
    ts: datetime
    accepted: bool = True
    duplicate: bool = False
    sessions_opened: int = 0
    sessions_closed: int = 0
    flights_opened: int = 0
    flights_closed: int = 0
    gap_seconds: float = 0.0
    rejected_reason: str | None = None


@dataclass(slots=True)
class _ActiveSession:
    key: str
    callsign: str
    started_at: datetime
    last_seen: datetime
    samples: int
    facility_code: int
    frequency_mhz: float | None


@dataclass(slots=True)
class _ActiveFlight:
    flight_id: int
    callsign: str
    plan: FlightPlan
    first_seen: datetime
    last_seen: datetime
    off_block: datetime | None
    max_gs_kt: int
    track: list[tuple[datetime, float, float, int | None, int | None, int | None]] = field(
        default_factory=list
    )
    last_sample: datetime | None = None


def _session_key(callsign: str, started_at: datetime) -> str:
    raw = f"{callsign}|{to_iso(started_at)}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


class Ingestor:
    """Stateful sessioniser. One instance per process, one process per database."""

    def __init__(
        self,
        db: Database,
        *,
        identity: Identity,
        reference: StaticReference | None = None,
        config: IngestConfig | None = None,
    ) -> None:
        self.db = db
        self.identity = identity
        self.reference = reference
        self.config = config or IngestConfig()
        self._prev_ts: datetime | None = None
        self._sessions: dict[str, _ActiveSession] = {}
        self._flights: dict[str, _ActiveFlight] = {}

    # ---------------------------------------------------------------- poll

    def poll(self, snapshot: Snapshot) -> PollResult:
        ts = snapshot.updated_at or datetime.now(UTC)
        result = PollResult(ts=ts)

        if self._prev_ts is not None and ts <= self._prev_ts:
            result.duplicate = True
            return result

        if not snapshot.pilots and not snapshot.controllers:
            result.accepted = False
            result.rejected_reason = "empty_feed"
            self.db.record_snapshot(
                {
                    "ts": to_iso(ts),
                    "feed_update": snapshot.update_raw,
                    "pilots": 0,
                    "accepted": False,
                    "reject_reason": "empty_feed",
                }
            )
            return result

        if self._prev_ts is not None:
            delta = (ts - self._prev_ts).total_seconds()
            if delta > self.config.interval_s * self.config.gap_factor:
                self.db.record_gap(self._prev_ts, ts, "poll_gap")
                result.gap_seconds = delta

        self.db.record_snapshot(
            {
                "ts": to_iso(ts),
                "feed_update": snapshot.update_raw,
                "version": snapshot.version,
                "connected_clients": snapshot.connected_clients,
                "unique_users": snapshot.unique_users,
                "pilots": len(snapshot.pilots),
                "controllers": len(snapshot.controllers),
                "atc_positions": len(snapshot.atc),
                "observers": len(snapshot.observers),
                "atis": len(snapshot.atis),
                "prefiled": snapshot.prefiled,
                "accepted": True,
            }
        )

        opened, closed = self._sessionise(snapshot, ts)
        flights_opened, flights_closed = self._track_flights(snapshot, ts)

        result.sessions_opened = opened
        result.sessions_closed = closed
        result.flights_opened = flights_opened
        result.flights_closed = flights_closed
        self._prev_ts = ts
        return result

    # ------------------------------------------------------------ sessions

    def _position_meta(self, controller: Controller) -> tuple[str | None, str | None]:
        position = controller.position
        if self.reference is not None:
            airport = self.reference.airport_for_stem(position.stem)
            return (airport.icao if airport else None), self.reference.resolve_fir(position.stem)
        return position.airport, position.fir

    def _sessionise(self, snapshot: Snapshot, ts: datetime) -> tuple[int, int]:
        seen: set[str] = set()
        opened = 0
        for controller in snapshot.atc:
            callsign = controller.callsign
            seen.add(callsign)
            active = self._sessions.get(callsign)
            if active is None:
                self._open_session(controller, ts)
                opened += 1
                continue
            active.last_seen = ts
            active.samples += 1
            if controller.frequency_mhz is not None:
                active.frequency_mhz = controller.frequency_mhz
            self.db.update_session(
                active.key,
                ended_at=ts,
                n_samples=active.samples,
                started_at=active.started_at,
            )

        closed = 0
        for callsign in [key for key in self._sessions if key not in seen]:
            active = self._sessions.pop(callsign)
            # The last poll already wrote ended_at = last_seen, so there is nothing to
            # finalise on close. This branch exists to forget the position.
            closed += 1
        return opened, closed

    def _open_session(self, controller: Controller, ts: datetime) -> None:
        start = self._prev_ts or ts
        merge_since = start - timedelta(seconds=self.config.reconnect_merge_s)
        recent = self.db.recent_session_for_callsign(controller.callsign, since=merge_since)

        if recent is not None:
            started_at = from_iso(recent["started_at"]) or start
            samples = int(recent["n_samples"]) + 1
            session_key = str(recent["session_key"])
            self.db.update_session(
                session_key, ended_at=ts, n_samples=samples, started_at=started_at
            )
        else:
            started_at = start
            samples = 1
            session_key = _session_key(controller.callsign, started_at)
            position_key = controller.position.key
            airport, fir = self._position_meta(controller)
            self.db.upsert_position(
                position_key,
                controller.position.facility.value,
                airport,
                fir,
                started_at,
                ts,
            )
            self.db.start_session(
                session_key=session_key,
                position_key=position_key,
                pseudo_id=self.identity.pseudonym(controller.cid),
                callsign=controller.callsign,
                facility_code=controller.facility_code,
                frequency_mhz=controller.frequency_mhz,
                rating=controller.rating,
                server=controller.server,
                started_at=started_at,
            )

        self._sessions[controller.callsign] = _ActiveSession(
            key=session_key,
            callsign=controller.callsign,
            started_at=started_at,
            last_seen=ts,
            samples=samples,
            facility_code=controller.facility_code,
            frequency_mhz=controller.frequency_mhz,
        )

    # ------------------------------------------------------------- flights

    def _track_flights(self, snapshot: Snapshot, ts: datetime) -> tuple[int, int]:
        seen: set[str] = set()
        opened = 0
        for pilot in snapshot.pilots:
            plan = pilot.flight_plan
            if plan is None or not plan.departure or not plan.arrival:
                continue
            callsign = pilot.callsign
            seen.add(callsign)
            active = self._flights.get(callsign)

            if active is not None and (
                active.plan.departure != plan.departure or active.plan.arrival != plan.arrival
            ):
                self._finish_flight(active, ts)
                active = None

            if active is None:
                active = self._open_flight(pilot, ts)
                opened += 1
            else:
                self._update_flight(active, pilot, ts)

        closed = 0
        for callsign in [key for key in self._flights if key not in seen]:
            self._finish_flight(self._flights.pop(callsign), ts)
            closed += 1
        return opened, closed

    def _open_flight(self, pilot: Pilot, ts: datetime) -> _ActiveFlight:
        plan = pilot.flight_plan
        assert plan is not None  # guarded by the caller
        start = self._prev_ts or ts
        flight_id = self.db.start_flight(
            callsign=pilot.callsign,
            pseudo_id=self.identity.pseudonym(pilot.cid),
            departure=plan.departure,
            arrival=plan.arrival,
            alternate=plan.alternate,
            aircraft_short=plan.aircraft_short,
            aircraft_full=plan.aircraft_full,
            flight_rules=plan.flight_rules,
            cruise_alt_ft=plan.cruise_altitude_ft,
            cruise_tas_kt=plan.cruise_tas_kt,
            filed_deptime=plan.deptime_hhmm,
            filed_enroute_min=plan.enroute_minutes,
            filed_route=plan.route,
            route_hash=parse_route(plan.route).route_hash,
            first_seen=start,
            max_gs_kt=pilot.groundspeed_kt,
        )
        active = _ActiveFlight(
            flight_id=flight_id,
            callsign=pilot.callsign,
            plan=plan,
            first_seen=start,
            last_seen=ts,
            off_block=start if pilot.airborne else None,
            max_gs_kt=pilot.groundspeed_kt,
        )
        if pilot.airborne and pilot.has_position:
            active.track.append(
                (
                    ts,
                    pilot.latitude,  # type: ignore[arg-type]
                    pilot.longitude,  # type: ignore[arg-type]
                    pilot.altitude_ft,
                    pilot.groundspeed_kt,
                    pilot.heading,
                )
            )
            active.last_sample = ts
        self._flights[pilot.callsign] = active
        return active

    def _update_flight(self, active: _ActiveFlight, pilot: Pilot, ts: datetime) -> None:
        active.last_seen = ts
        active.max_gs_kt = max(active.max_gs_kt, pilot.groundspeed_kt)
        if active.off_block is None and pilot.airborne:
            active.off_block = ts

        if not pilot.airborne or not pilot.has_position:
            self.db.update_flight(
                active.flight_id,
                last_seen=ts,
                max_gs_kt=active.max_gs_kt,
                off_block=active.off_block,
            )
            return

        due = (
            active.last_sample is None
            or (ts - active.last_sample).total_seconds() >= self.config.track_sample_s
        )
        if due:
            active.track.append(
                (
                    ts,
                    pilot.latitude,  # type: ignore[arg-type]
                    pilot.longitude,  # type: ignore[arg-type]
                    pilot.altitude_ft,
                    pilot.groundspeed_kt,
                    pilot.heading,
                )
            )
            active.last_sample = ts

        self.db.update_flight(
            active.flight_id,
            last_seen=ts,
            max_gs_kt=active.max_gs_kt,
            off_block=active.off_block,
        )

    def _finish_flight(self, active: _ActiveFlight, ts: datetime) -> None:
        if active.track:
            points = [(row[1], row[2]) for row in active.track]
            kept = [active.track[i] for i in rdp_keep_indices(points, self.config.track_tolerance_nm)]
        else:
            kept = []
        self.db.append_track(active.flight_id, kept)
        self.db.finish_flight(
            active.flight_id,
            last_seen=active.last_seen,
            off_block=active.off_block,
            on_block=active.last_seen,
            n_track_points=len(kept),
        )

    # --------------------------------------------------------------- misc

    def finalize(self) -> None:
        """Flush in-memory state on a clean shutdown."""
        for active in list(self._flights.values()):
            self._finish_flight(active, active.last_seen)
        self._flights.clear()
        for callsign in list(self._sessions):
            session = self._sessions.pop(callsign)
            self.db.update_session(
                session.key,
                ended_at=session.last_seen,
                n_samples=session.samples,
                started_at=session.started_at,
            )


def _log_line(result: PollResult) -> str:
    if result.duplicate:
        return f"{to_iso(result.ts)} duplicate snapshot ignored"
    if not result.accepted:
        return f"{to_iso(result.ts)} rejected: {result.rejected_reason}"
    gap = f" gap={int(result.gap_seconds)}s" if result.gap_seconds else ""
    return (
        f"{to_iso(result.ts)} ok"
        f" sessions +{result.sessions_opened}/-{result.sessions_closed}"
        f" flights +{result.flights_opened}/-{result.flights_closed}{gap}"
    )


def run_ingest(
    settings: Settings,
    *,
    once: bool = False,
    minutes: float = 0.0,
    interval_s: int | None = None,
    quiet: bool = False,
    max_polls: int | None = None,
) -> int:
    """Poll the live feed until told to stop. Returns a process exit code."""
    settings.ensure_dirs()
    db = Database(settings.data_dir / "skysherpa.sqlite3")
    db.initialize()
    identity = Identity.load_or_create(settings.data_dir / "identity.key")

    reference: StaticReference | None = None
    try:
        reference = load_reference(settings)
    except RuntimeError as exc:  # network down on first run; degrade gracefully
        if not quiet:
            print(f"static reference unavailable ({exc}); FIR resolution disabled")

    interval = interval_s or settings.poll_seconds
    config = IngestConfig(interval_s=interval)
    ingestor = Ingestor(db, identity=identity, reference=reference, config=config)

    # Flights left open by a previous crash are closed at their last honest sighting.
    orphans = db.close_orphan_flights(before=datetime.now(UTC) - timedelta(minutes=10))
    if orphans and not quiet:
        print(f"closed {orphans} flight(s) left open by a previous run")

    started = time.monotonic()
    polls = 0
    try:
        while True:
            try:
                snapshot = fetch_snapshot(settings)
            except FeedError as exc:
                if not quiet:
                    print(f"feed error: {exc}")
                if once:
                    return 2
                time.sleep(interval)
                continue

            result = ingestor.poll(snapshot)
            polls += 1
            if not quiet:
                print(_log_line(result), flush=True)

            if once or (max_polls is not None and polls >= max_polls):
                break
            if minutes and (time.monotonic() - started) >= minutes * 60:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        if not quiet:
            print("interrupted; flushing state")
    finally:
        ingestor.finalize()

    if not quiet:
        stats = db.stats()
        print(
            f"stopped after {polls} poll(s): {stats['snapshots']:,} snapshots, "
            f"{stats['sessions']:,} sessions, {stats['flights']:,} flights, "
            f"{stats['bytes'] / 1_048_576:.1f} MB"
        )
    db.close()
    return 0
