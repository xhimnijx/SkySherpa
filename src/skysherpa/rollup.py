"""Roll raw observations into the aggregates the models read.

Three products:

* ``position_slot`` -- the training matrix: was this position online in this 15-minute
  slot on this day?
* ``pair_stat`` / ``route_variant`` -- the route corpus: which city pairs and which route
  strings people actually fly, and how long they take.
* ``airport_hourly`` -- observed departures and arrivals per airport-hour, the raw
  material for the congestion model.

**Absence is data.** The first version of this module only wrote a row when a position
was online, which taught the model that every position is online 100% of the time. A slot
is only evidence of absence if we were actually polling then, so observability comes from
``snapshot_stat``: every polled slot becomes a row for every position that existed at the
time, online or not. That is the whole trick, and it is why the coverage numbers mean
anything at all.

Rollups are idempotent: they rebuild from the session and flight tables, so running them
twice is harmless and running them after a parser fix repairs history.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from .geo import distance_nm
from .staticdata import StaticReference
from .store import Database, from_iso

__all__ = [
    "RollupResult",
    "rebuild_all",
    "rebuild_airport_hourly",
    "rebuild_pairs",
    "rebuild_route_variants",
    "rebuild_slots",
]

SLOT = timedelta(minutes=15)
SLOTS_PER_DAY = 96
#: A slot counts as "online" when the position was staffed for at least half of it.
ONLINE_THRESHOLD_MIN = 7.5
#: How far back the materialised matrix goes. Older slots are pruned so the table stays
#: laptop-sized; the raw sessions are still in the database if we ever need them.
DEFAULT_WINDOW_DAYS = 120
#: How many trailing days a rebuild recomputes. Only the tail of history can change, and a
#: 120-day matrix at network scale is over a million rows: rewriting it on every publish is
#: pure churn.
DEFAULT_REFRESH_DAYS = 3
#: Rows per insert statement. The first implementation accumulated the whole matrix in one
#: list, which is a laptop-sized memory spike for no benefit.
INSERT_BATCH = 20_000
EMPTY_SET: frozenset[str] = frozenset()
SLOT_INSERT = (
    "INSERT OR REPLACE INTO position_slot (position_key, day, slot, online, n_samples) "
    "VALUES (?,?,?,?,?)"
)


@dataclass(slots=True)
class RollupResult:
    slots: int = 0
    pairs: int = 0
    airports: int = 0
    variants: int = 0
    window_days: int = 0


def _floor_slot(moment: datetime) -> datetime:
    return moment.replace(minute=(moment.minute // 15) * 15, second=0, microsecond=0)


def _slot_index(moment: datetime) -> int:
    return moment.hour * 4 + moment.minute // 15


def _slot_start(day: date, slot: int) -> datetime:
    return datetime.combine(day, time(hour=slot // 4, minute=(slot % 4) * 15), tzinfo=UTC)


def rebuild_slots(
    db: Database,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    refresh_days: int = DEFAULT_REFRESH_DAYS,
) -> int:
    """Rebuild the presence matrix, including the slots where nothing was online.

    History is immutable, so only the last ``refresh_days`` are recomputed. A 120-day
    matrix at network scale is over a million rows; rewriting all of them on every
    fifteen-minute publish cost 14 seconds of pure churn in the rehearsal and produced the
    same answer it produced the time before. ``refresh_days >= window_days`` forces the
    full path, which is what a first run and the tests use.

    Rows are inserted in batches rather than accumulated into one list: the list version
    held every row at once, which is a laptop-sized memory spike for no reason.
    """
    # 1. When were we actually watching?
    polls: Counter[tuple[date, int]] = Counter()
    for row in db.query("SELECT ts FROM snapshot_stat WHERE accepted = 1"):
        moment = from_iso(row["ts"])
        if moment is None:
            continue
        polls[(moment.date(), _slot_index(moment))] += 1
    if not polls:
        db.execute("DELETE FROM position_slot")
        return 0

    latest = max(day for day, _ in polls)
    cutoff = latest - timedelta(days=window_days)
    observed = {key: count for key, count in polls.items() if key[0] >= cutoff}

    # 2. How many minutes was each position online, per slot?
    minutes: dict[tuple[str, date, int], float] = defaultdict(float)
    for row in db.query("SELECT position_key, started_at, ended_at FROM controller_session"):
        start = from_iso(row["started_at"])
        end = from_iso(row["ended_at"])
        if start is None or end is None or end <= start:
            continue
        cursor = _floor_slot(start)
        while cursor < end:
            following = cursor + SLOT
            overlap = (min(end, following) - max(start, cursor)).total_seconds() / 60.0
            if overlap > 0:
                key = (str(row["position_key"]), cursor.date(), _slot_index(cursor))
                minutes[key] += overlap
            cursor = following

    online_by_slot: dict[tuple[date, int], set[str]] = defaultdict(set)
    for (position, day, slot), total in minutes.items():
        if total >= ONLINE_THRESHOLD_MIN:
            online_by_slot[(day, slot)].add(position)

    # 3. Which positions existed when? A position that first appeared yesterday was not
    #    "offline" for the previous three months; it did not exist.
    lifetimes: dict[str, datetime] = {}
    for row in db.query("SELECT position_key, first_seen FROM position"):
        first_seen = from_iso(row["first_seen"])
        if first_seen is not None:
            lifetimes[str(row["position_key"])] = first_seen

    full = refresh_days >= window_days
    recent_from = latest - timedelta(days=max(0, refresh_days - 1))
    if not full:
        # A database with nothing older than the refresh window has to be built from
        # scratch; otherwise the first run after this optimisation writes only three days
        # and the matrix silently loses its history.
        older = int(
            db.scalar(
                "SELECT COUNT(*) FROM position_slot WHERE day < ?", (recent_from.isoformat(),)
            )
            or 0
        )
        full = older == 0
    db.execute("DELETE FROM position_slot WHERE day < ?", (cutoff.isoformat(),))
    if full:
        db.execute("DELETE FROM position_slot")
    else:
        db.execute("DELETE FROM position_slot WHERE day >= ?", (recent_from.isoformat(),))

    batch: list[tuple[str, str, int, int, int]] = []

    def flush() -> None:
        if batch:
            db.executemany(SLOT_INSERT, batch)
            batch.clear()

    for (day, slot), count in observed.items():
        if not full and day < recent_from:
            continue
        start = _slot_start(day, slot)
        end = start + SLOT
        iso_day = day.isoformat()
        online = online_by_slot.get((day, slot), EMPTY_SET)
        for position, first_seen in lifetimes.items():
            if first_seen > end:
                continue
            batch.append((position, iso_day, slot, 1 if position in online else 0, count))
            if len(batch) >= INSERT_BATCH:
                flush()
    flush()
    return int(db.scalar("SELECT COUNT(*) FROM position_slot") or 0)


def rebuild_pairs(db: Database, reference: StaticReference | None = None) -> int:
    """Rebuild per-city-pair statistics from *filed intent*, not just completed flights.

    A fresh install has thousands of aircraft in the air and almost no completed flights
    yet. Filtering on ``block_min`` here meant the website had no candidate routes at all
    for the first several hours -- a cold start that looked like a bug. Every observed
    flight with a filed origin and destination counts; the median block time is simply
    unknown until some of them land.
    """
    rows = db.query(
        """
        SELECT dep_icao, arr_icao, COUNT(*) AS n, AVG(block_min) AS avg_block,
               MAX(last_seen) AS last_seen
        FROM flight
        WHERE dep_icao <> '' AND arr_icao <> '' AND dep_icao <> arr_icao
        GROUP BY dep_icao, arr_icao
        """
    )
    payload = []
    for row in rows:
        dep, arr = str(row["dep_icao"]), str(row["arr_icao"])
        distance = None
        if reference is not None:
            a = reference.airports.get(dep)
            b = reference.airports.get(arr)
            if a is not None and b is not None:
                distance = round(distance_nm((a.lat, a.lon), (b.lat, b.lon)), 1)
        payload.append(
            (
                dep,
                arr,
                int(row["n"]),
                distance,
                int(row["avg_block"]) if row["avg_block"] is not None else None,
                row["last_seen"],
            )
        )
    db.execute("DELETE FROM pair_stat")
    db.executemany(
        "INSERT OR REPLACE INTO pair_stat (dep_icao, arr_icao, n_flights, distance_nm, "
        "med_block_min, last_seen) VALUES (?,?,?,?,?,?)",
        payload,
    )
    return len(payload)


def _hour_bucket(timestamp: str | None) -> str | None:
    if not timestamp or len(timestamp) < 13:
        return None
    return f"{timestamp[:13]}:00:00Z"


def rebuild_airport_hourly(db: Database) -> int:
    """Observed departures and arrivals per airport-hour."""
    buckets: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"departures": 0, "arrivals": 0}
    )

    for row in db.query(
        "SELECT dep_icao, off_block FROM flight "
        "WHERE off_block IS NOT NULL AND dep_icao <> ''"
    ):
        hour = _hour_bucket(row["off_block"])
        if hour:
            buckets[(str(row["dep_icao"]), hour)]["departures"] += 1

    for row in db.query(
        "SELECT arr_icao, on_block FROM flight "
        "WHERE on_block IS NOT NULL AND arr_icao <> ''"
    ):
        hour = _hour_bucket(row["on_block"])
        if hour:
            buckets[(str(row["arr_icao"]), hour)]["arrivals"] += 1

    db.execute("DELETE FROM airport_hourly")
    db.executemany(
        "INSERT OR REPLACE INTO airport_hourly (icao, hour_utc, departures, arrivals) "
        "VALUES (?,?,?,?)",
        [
            (icao, hour, counts["departures"], counts["arrivals"])
            for (icao, hour), counts in buckets.items()
        ],
    )
    return len(buckets)


def rebuild_route_variants(db: Database) -> int:
    rows = db.query(
        """
        SELECT route_hash, dep_icao, arr_icao, MIN(filed_route) AS route,
               COUNT(*) AS n, MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen,
               AVG(cruise_alt_ft) AS avg_alt, AVG(block_min) AS avg_block
        FROM flight
        WHERE route_hash IS NOT NULL AND route_hash <> ''
        GROUP BY route_hash
        """
    )
    payload = [
        (
            str(row["route_hash"]),
            str(row["dep_icao"]),
            str(row["arr_icao"]),
            row["route"] or "",
            int(row["n"]),
            row["first_seen"],
            row["last_seen"],
            int(row["avg_alt"]) if row["avg_alt"] is not None else None,
            int(row["avg_block"]) if row["avg_block"] is not None else None,
            None,
        )
        for row in rows
    ]
    db.execute("DELETE FROM route_variant")
    db.executemany(
        "INSERT OR REPLACE INTO route_variant (route_hash, dep_icao, arr_icao, normalized, "
        "n_observations, first_seen, last_seen, med_alt_ft, med_block_min, distance_nm) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        payload,
    )
    return len(payload)


def rebuild_all(
    db: Database,
    reference: StaticReference | None = None,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> RollupResult:
    return RollupResult(
        slots=rebuild_slots(db, window_days=window_days),
        pairs=rebuild_pairs(db, reference),
        airports=rebuild_airport_hourly(db),
        variants=rebuild_route_variants(db),
        window_days=window_days,
    )
