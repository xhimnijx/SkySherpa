"""A synthetic VATSIM network, for developing the forecaster before real history exists.

The hardest constraint on this project is the calendar: a six-week forecast gate cannot be
compressed, and the real network only produces a day of data per day. This module builds a
believable network with *known* schedules so that P2 and P3 can be built, tested in CI,
and calibrated against ground truth months before the real dataset is deep enough.

It is also the honest way to test the hard cases: a controller who is 95% reliable but only
ever online in one three-hour block, a position that goes dark in week three, a weekend-only
tower. Those are exactly where a naive model fails, and they are free to manufacture.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from .identity import Identity
from .store import Database, to_iso

__all__ = ["SimPosition", "SimResult", "simulate_flights", "simulate_network"]


@dataclass(frozen=True, slots=True)
class SimPosition:
    callsign: str
    facility_code: int
    weekdays: tuple[int, ...]  # 0 = Monday
    start_hour: int
    duration_hours: float
    reliability: float
    fir: str = ""


@dataclass(slots=True)
class SimResult:
    positions: int = 0
    sessions: int = 0
    flights: int = 0
    days: int = 0


#: A small but varied network: daily towers, a weekday-only centre, a weekend-only
#: approach, a position that appears only late, and one that stops showing up.
DEFAULT_NETWORK: tuple[SimPosition, ...] = (
    SimPosition("EGLL_GND", 3, (0, 1, 2, 3, 4, 5, 6), 16, 5.5, 0.86, "EGTT"),
    SimPosition("EGLL_TWR", 4, (0, 1, 2, 3, 4, 5, 6), 16, 5.5, 0.84, "EGTT"),
    SimPosition("EGLL_APP", 5, (0, 1, 2, 3, 4, 5, 6), 16, 5.0, 0.78, "EGTT"),
    SimPosition("EGTT_CTR", 6, (0, 1, 2, 3, 4), 17, 5.0, 0.72, "EGTT"),
    SimPosition("LON_S_CTR", 6, (1, 3, 5), 19, 3.5, 0.55, "EGTT"),
    SimPosition("EDDF_TWR", 4, (0, 1, 2, 3, 4, 5, 6), 15, 6.0, 0.74, "EDGG"),
    SimPosition("EDGG_CTR", 6, (0, 1, 2, 3, 4, 5), 16, 5.5, 0.66, "EDGG"),
    SimPosition("LFPG_TWR", 4, (0, 2, 4, 6), 17, 4.0, 0.58, "LFFF"),
    SimPosition("LFFF_CTR", 6, (0, 2, 4), 18, 4.0, 0.48, "LFFF"),
    SimPosition("EHAM_TWR", 4, (0, 1, 2, 3, 4, 5, 6), 17, 4.0, 0.70, "EHAA"),
    SimPosition("KZMA_CTR", 6, (0, 1, 2, 3, 4, 5, 6), 20, 6.0, 0.45, "KZMA"),
    SimPosition("KJFK_APP", 5, (5, 6), 15, 6.0, 0.42, "KZNY"),
)

_AIRPORTS = [
    ("EGLL", "LEPA", 145),
    ("EGLL", "EHAM", 55),
    ("EGLL", "EDDF", 80),
    ("EGPH", "EGLL", 70),
    ("EDDF", "LEPA", 125),
    ("EHAM", "LFPG", 60),
    ("LFPG", "LEMD", 105),
    ("KJFK", "KBOS", 55),
    ("KLAX", "KSFO", 75),
    ("KATL", "KMIA", 95),
    ("KORD", "KDFW", 130),
    ("KDEN", "KSEA", 140),
    ("EGLL", "LFPG", 70),
    ("EGLL", "EIDW", 75),
    ("EDDF", "LOWW", 80),
    ("LSZH", "LIRF", 85),
    ("EKCH", "ENGM", 70),
    ("ESSA", "EFHK", 75),
    ("EPWA", "EDDM", 85),
    ("LPPT", "LEMD", 75),
    ("LGAV", "LIRF", 110),
    ("EGKK", "LEPA", 130),
    ("EGSS", "EHAM", 60),
    ("LEMG", "EGLL", 150),
    ("KJFK", "KORD", 140),
    ("KSEA", "KSFO", 110),
    ("KMIA", "KATL", 100),
    ("CYYZ", "KJFK", 80),
    ("VHHH", "RJTT", 210),
    ("YSSY", "YMML", 90),
    ("EGLL", "LEMD", 150),
    ("EDDF", "LSZH", 60),
    ("LEPA", "LFPG", 120),
    ("EHAM", "EGKK", 55),
    ("KLAX", "KDEN", 120),
    ("KDFW", "KATL", 115),
]


def _ensure_position(db: Database, spec: SimPosition, start: datetime, end: datetime) -> None:
    facility = {3: "GND", 4: "TWR", 5: "APP", 6: "CTR"}.get(spec.facility_code, "UNK")
    airport = spec.callsign.split("_")[0]
    db.upsert_position(spec.callsign.upper(), facility, airport, spec.fir or None, start, end)


def simulate_network(
    db: Database,
    *,
    identity: Identity | None = None,
    days: int = 56,
    seed: int = 11,
    end: datetime | None = None,
    network: tuple[SimPosition, ...] = DEFAULT_NETWORK,
    dropout_after_days: int | None = 40,
) -> SimResult:
    """Insert plausible controller sessions directly into the store.

    ``dropout_after_days`` retires one position partway through the run, which is the
    case that separates a model that adapts from one that averages a dead position into
    its optimism forever.
    """
    identity = identity or Identity.from_secret("simulator")
    rng = random.Random(seed)
    end = (end or datetime.now(tz=None)).replace(minute=0, second=0, microsecond=0)
    start_day = (end - timedelta(days=days)).date()

    result = SimResult(days=days, positions=len(network))
    for spec in network:
        _ensure_position(db, spec, datetime.combine(start_day, datetime.min.time(), tzinfo=end.tzinfo), end)

    for offset in range(days):
        day = start_day + timedelta(days=offset)
        weekday = day.weekday()
        for spec in network:
            if weekday not in spec.weekdays:
                continue
            if dropout_after_days is not None and spec.callsign == "LON_S_CTR":
                if offset > dropout_after_days:
                    continue
            if rng.random() > spec.reliability:
                continue

            jitter_start = rng.randint(-20, 40)
            start = datetime(
                day.year, day.month, day.day, spec.start_hour, 0, tzinfo=end.tzinfo
            ) + timedelta(minutes=jitter_start)
            duration = spec.duration_hours * 60 + rng.randint(-25, 35)
            finish = start + timedelta(minutes=max(45, duration))

            session_key = f"sim-{spec.callsign}-{to_iso(start)}"
            db.start_session(
                session_key=session_key[:64],
                position_key=spec.callsign.upper(),
                pseudo_id=identity.pseudonym(abs(hash(spec.callsign)) % 100000),
                callsign=spec.callsign.upper(),
                facility_code=spec.facility_code,
                frequency_mhz=118.0 + (spec.facility_code / 10),
                rating=5,
                server="SIM",
                started_at=start,
            )
            db.update_session(
                session_key=session_key[:64],
                ended_at=finish,
                n_samples=max(2, int((finish - start).total_seconds() // 30)),
                started_at=start,
            )
            result.sessions += 1

    # The rollup needs to know when we were watching. Without these rows, every
    # observed slot looks online-only and the forecaster learns nothing.
    _simulate_observability(db, start_day=start_day, days=days, end=end, rng=rng)
    return result


def _simulate_observability(
    db: Database, *, start_day, days: int, end: datetime, rng: random.Random
) -> None:
    rows = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        for slot in range(96):
            moment = datetime(day.year, day.month, day.day, 0, 0, tzinfo=end.tzinfo) + timedelta(
                minutes=slot * 15
            )
            rows.append(
                (
                    to_iso(moment),
                    "sim",
                    3,
                    rng.randint(850, 1250),
                    rng.randint(800, 1200),
                    rng.randint(900, 1200),
                    rng.randint(80, 120),
                    rng.randint(35, 60),
                    rng.randint(60, 95),
                    rng.randint(80, 160),
                    1,  # prefiled
                    1,  # accepted
                )
            )
    db.executemany(
        "INSERT OR REPLACE INTO snapshot_stat (ts, feed_update, version, connected_clients, "
        "unique_users, pilots, controllers, atc_positions, observers, atis, prefiled, accepted) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def simulate_flights(
    db: Database,
    *,
    identity: Identity | None = None,
    days: int = 56,
    per_day: int = 22,
    seed: int = 23,
    end: datetime | None = None,
) -> int:
    """Insert completed flights so the route and demand rollups have something to chew on."""
    identity = identity or Identity.from_secret("simulator")
    rng = random.Random(seed)
    end = (end or datetime.now(tz=None)).replace(minute=0, second=0, microsecond=0)
    start_day = (end - timedelta(days=days)).date()
    written = 0

    for offset in range(days):
        day = start_day + timedelta(days=offset)
        for index in range(per_day):
            dep, arr, block = rng.choice(_AIRPORTS)
            block_min = max(25, block + rng.randint(-15, 25))
            hour = rng.choice([7, 9, 12, 15, 17, 19, 21])
            off_block = datetime(
                day.year, day.month, day.day, hour, rng.randint(0, 50), tzinfo=end.tzinfo
            )
            on_block = off_block + timedelta(minutes=block_min)
            callsign = f"SIM{offset:02d}{index:02d}"

            # Roughly what people fly on a sector this long, so the published bundle's
            # "what does the network actually fly here" field has something to say.
            if block_min <= 45:
                aircraft_short, aircraft_full, alt, tas = rng.choice(
                    [
                        ("C172", "C172/L", 8000, 120),
                        ("DH8D", "DH8D/M", 20000, 280),
                        ("PA28", "PA28/L", 6000, 125),
                    ]
                )
            elif block_min <= 130:
                aircraft_short, aircraft_full, alt, tas = rng.choice(
                    [
                        ("DH8D", "DH8D/M", 20000, 280),
                        ("E195", "E195/M", 33000, 440),
                        ("A20N", "A20N/M-S", 35000, 450),
                        ("AT76", "AT76/M", 18000, 275),
                    ]
                )
            else:
                aircraft_short, aircraft_full, alt, tas = rng.choice(
                    [
                        ("A20N", "A20N/M-S", 35000, 450),
                        ("B738", "B738/M", 36000, 430),
                        ("A359", "A359/H", 38000, 480),
                        ("B77W", "B77W/H", 37000, 480),
                    ]
                )

            flight_id = db.start_flight(
                callsign=callsign,
                pseudo_id=identity.pseudonym(index),
                departure=dep,
                arrival=arr,
                alternate="",
                aircraft_short=aircraft_short,
                aircraft_full=aircraft_full,
                flight_rules="I",
                cruise_alt_ft=alt,
                cruise_tas_kt=tas,
                filed_deptime=f"{hour:02d}{rng.randint(0, 59):02d}",
                filed_enroute_min=block_min,
                filed_route=f"DCT {dep} DCT {arr}",
                route_hash=f"{dep}{arr}"[:16],
                first_seen=off_block,
                max_gs_kt=430,
            )
            db.finish_flight(
                flight_id,
                last_seen=on_block,
                off_block=off_block,
                on_block=on_block,
                n_track_points=0,
            )
            written += 1
    return written
