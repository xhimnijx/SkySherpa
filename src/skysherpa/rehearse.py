"""Scale rehearsal: build a long synthetic history and time the whole pipeline.

The product's last gate is a calendar one, so the temptation is to wait 34 days and hope
the model, the rollup and the bundle all still work at that size. This command manufactures
the waiting instead: it generates a network and a history of any depth, then times every
stage and reports the storage cost.

It is also the tool that tells you whether your laptop can host the thing.
"""

from __future__ import annotations

import random
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .export import build_bundle
from .oracle import CoverageModel, PresenceMatrix, latest_calibrator, rolling_origin
from .rollup import rebuild_all
from .simulate import SimPosition, simulate_flights, simulate_network
from .staticdata import load_reference
from .store import Database

__all__ = ["RehearsalReport", "build_network", "run_rehearsal"]

#: (airport, FIR) pairs, roughly the busiest corners of the network.
AIRPORTS: tuple[tuple[str, str], ...] = (
    ("EGLL", "EGTT"), ("EGKK", "EGTT"), ("EGPH", "EGPX"), ("EIDW", "EISN"),
    ("EDDF", "EDGG"), ("EDDM", "EDMM"), ("EDDH", "EDWW"), ("EDDK", "EDGG"),
    ("EHAM", "EHAA"), ("LFPG", "LFFF"), ("LFLL", "LFMM"), ("LSZH", "LSAS"),
    ("LOWW", "LOVV"), ("LEMD", "LECM"), ("LEBL", "LECB"), ("LIRF", "LIRR"),
    ("LIMC", "LIMM"), ("EKCH", "EKDK"), ("ENGM", "ENOR"), ("ESSA", "ESOS"),
    ("EFHK", "EFIN"), ("EPWA", "EPWW"), ("LKPR", "LKAA"), ("LPPT", "LPPC"),
    ("LGAV", "LGGG"), ("LTFM", "LTAA"), ("OMDB", "OMAE"), ("KJFK", "KZNY"),
    ("KBOS", "KZBW"), ("KATL", "KZTL"), ("KMIA", "KZMA"), ("KORD", "KZAU"),
    ("KDFW", "KZFW"), ("KDEN", "KZDV"), ("KLAX", "KZLA"), ("KSFO", "KZOA"),
    ("KSEA", "KZSE"), ("CYYZ", "CZYZ"), ("KPHX", "KZAB"), ("KBNA", "KZME"),
)


@dataclass(slots=True)
class RehearsalReport:
    days: int = 0
    airports: int = 0
    positions: int = 0
    sessions: int = 0
    flights: int = 0
    presence_rows: int = 0
    db_bytes: int = 0
    bundle_bytes: int = 0
    simulate_seconds: float = 0.0
    rollup_seconds: float = 0.0
    rollup_steady_seconds: float = 0.0
    model_seconds: float = 0.0
    calibrate_seconds: float = 0.0
    backtest_seconds: float = 0.0
    publish_seconds: float = 0.0
    brier_skill: float = 0.0

    def summary(self) -> str:
        mb = self.db_bytes / 1_048_576
        bundle = self.bundle_bytes / 1024
        return "\n".join(
            [
                f"rehearsal: {self.days} days, {self.airports} airports, "
                f"{self.positions} positions",
                f"  history    {self.sessions:,} sessions | {self.flights:,} flights | "
                f"{self.presence_rows:,} presence rows",
                f"  storage    {mb:.0f} MB database | {bundle:.0f} KB bundle",
                f"  timings    simulate {self.simulate_seconds:.1f}s | "
                f"rollup {self.rollup_seconds:.1f}s "
                f"(steady state {self.rollup_steady_seconds:.1f}s) | "
                f"model {self.model_seconds:.2f}s",
                f"             calibrate {self.calibrate_seconds:.2f}s | "
                f"backtest {self.backtest_seconds:.1f}s | publish {self.publish_seconds:.1f}s",
                f"  forecast   brier skill {self.brier_skill:.3f} on the oldest fold",
            ]
        )


def build_network(airports: int, seed: int = 7) -> tuple[SimPosition, ...]:
    """A plausible network: a tower and a weekday approach at each airport, plus a
    centre for each FIR."""
    rng = random.Random(seed)
    chosen = AIRPORTS[: max(1, min(airports, len(AIRPORTS)))]
    specs: list[SimPosition] = []
    for airport, fir in chosen:
        start = rng.choice([14, 15, 16, 17])
        specs.append(
            SimPosition(
                f"{airport}_TWR", 4, tuple(range(7)), start, rng.uniform(3.5, 6.0),
                rng.uniform(0.5, 0.85), fir,
            )
        )
        specs.append(
            SimPosition(
                f"{airport}_APP", 5, tuple(range(5)), start, rng.uniform(3.0, 5.0),
                rng.uniform(0.35, 0.7), fir,
            )
        )
    for fir in sorted({fir for _, fir in chosen}):
        specs.append(SimPosition(f"{fir}_CTR", 6, tuple(range(7)), 14, 8.0, 0.45, fir))
    return tuple(specs)


def run_rehearsal(
    *,
    days: int = 120,
    airports: int = 40,
    flights_per_day: int = 150,
    seed: int = 7,
    reference=None,
    out_dir: Path | None = None,
) -> RehearsalReport:
    report = RehearsalReport(days=days, airports=airports)
    network = build_network(airports, seed=seed)
    report.positions = len(network)

    directory = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="skysherpa-rehearsal-"))
    database = Database(directory / "rehearsal.sqlite3")
    database.initialize()
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

    started = time.monotonic()
    simulation = simulate_network(database, days=days, seed=seed, end=end, network=network)
    flights = simulate_flights(
        database, days=days, per_day=flights_per_day, seed=seed + 1, end=end
    )
    report.simulate_seconds = time.monotonic() - started
    report.sessions = simulation.sessions
    report.flights = flights

    started = time.monotonic()
    rebuild_all(database)
    report.rollup_seconds = time.monotonic() - started
    report.presence_rows = int(
        database.scalar("SELECT COUNT(*) FROM position_slot") or 0
    )
    # The second pass is what a fifteen-minute publish actually pays: only the tail of
    # history is recomputed. The first pass is the one-off backfill.
    started = time.monotonic()
    rebuild_all(database)
    report.rollup_steady_seconds = time.monotonic() - started

    matrix = PresenceMatrix.from_db(database)
    started = time.monotonic()
    model = CoverageModel.fit(matrix, reference_date=end.date())
    report.model_seconds = time.monotonic() - started

    started = time.monotonic()
    latest_calibrator(matrix)
    report.calibrate_seconds = time.monotonic() - started

    started = time.monotonic()
    backtest = rolling_origin(matrix, folds=4, horizon_days=7)
    report.backtest_seconds = time.monotonic() - started
    report.brier_skill = backtest.brier_skill

    started = time.monotonic()
    result = build_bundle(
        database,
        out_dir=directory / "dist",
        reference=reference,
        reference_date=end.date(),
        write_web_assets=False,
    )
    report.publish_seconds = time.monotonic() - started
    report.bundle_bytes = result.total_bytes
    report.db_bytes = database.path.stat().st_size

    database.close()
    if out_dir is None:
        shutil.rmtree(directory, ignore_errors=True)
    return report


def default_reference():
    """Use the local cache rather than downloading mid-rehearsal."""
    from .config import Settings

    settings = Settings.load()
    try:
        return load_reference(settings)
    except RuntimeError:
        return None
