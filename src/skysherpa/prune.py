"""Bounded retention for a collector that is supposed to run for months.

What has to be kept forever and what does not:

* ``controller_session`` and ``snapshot_stat`` are the forecaster's raw material and are
  tiny (one row per controller stint, one per poll). They are never pruned.
* ``flight`` and ``flight_track`` are the bulk: roughly 2,500 rows per hour of collection,
  most of them pilots who appeared for a few minutes. They only feed the *route corpus*
  (popularity, airport activity, route variants).
* ``position_slot`` is a materialised aggregate and survives pruning untouched, which is
  what makes pruning safe: the forecast keeps its whole history even when the raw flights
  behind the route counts are gone.

Pruning therefore shortens the *route popularity* window and nothing else. That is a
feature rather than a compromise -- "which route did people fly" is a question about recent
behaviour, and a 30-day window answers it better than a 120-day one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .store import Database

__all__ = ["DEFAULT_KEEP_DAYS", "PruneResult", "prune_history"]

#: Flights older than this are removed by default. Long enough for a monthly route
#: popularity read, short enough that the database stays laptop-sized.
DEFAULT_KEEP_DAYS = 45


@dataclass(slots=True)
class PruneResult:
    keep_days: int
    cutoff: str
    flights: int = 0
    tracks: int = 0
    bytes_before: int = 0
    bytes_after: int = 0

    @property
    def reclaimed(self) -> int:
        return max(0, self.bytes_before - self.bytes_after)

    def summary(self) -> str:
        mb = self.reclaimed / 1_048_576
        days = f"{self.keep_days} day{'s' if self.keep_days != 1 else ''}"
        return (
            f"kept the last {days} (before {self.cutoff}): "
            f"pruned {self.flights:,} flights and {self.tracks:,} track points, "
            f"reclaimed {mb:.1f} MB"
        )


def prune_history(
    db: Database,
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    now: datetime | None = None,
    dry_run: bool = False,
) -> PruneResult:
    if keep_days < 1:
        raise ValueError("keep_days must be at least 1")
    moment = now or datetime.now(UTC)
    cutoff = (moment - timedelta(days=keep_days)).replace(microsecond=0)

    result = PruneResult(keep_days=keep_days, cutoff=cutoff.isoformat())
    result.bytes_before = db.path.stat().st_size if db.path.exists() else 0

    flights = int(
        db.scalar("SELECT COUNT(*) FROM flight WHERE last_seen < ?", (cutoff.isoformat(),)) or 0
    )
    tracks = int(
        db.scalar(
            "SELECT COUNT(*) FROM flight_track WHERE flight_id IN "
            "(SELECT flight_id FROM flight WHERE last_seen < ?)",
            (cutoff.isoformat(),),
        )
        or 0
    )
    result.flights = flights
    result.tracks = tracks
    if dry_run or (flights == 0 and tracks == 0):
        result.bytes_after = result.bytes_before
        return result

    db.execute(
        "DELETE FROM flight_track WHERE flight_id IN "
        "(SELECT flight_id FROM flight WHERE last_seen < ?)",
        (cutoff.isoformat(),),
    )
    db.execute("DELETE FROM flight WHERE last_seen < ?", (cutoff.isoformat(),))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("VACUUM")
    result.bytes_after = db.path.stat().st_size
    return result
