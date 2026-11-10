"""Command line interface.

P0 commands are diagnostic, but not toys: `sniff --coverage` is the first honest slice
of the product -- it joins live traffic to FIR boundaries and shows the gap between
where the aircraft are and where the controllers are.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import platform
import signal
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import __version__, doctor
from .aircraft import CLASSES, ENGINES, SIMS, AircraftCatalog, load_catalog
from .callsign import parse as parse_callsign
from .config import Settings
from .export import build_bundle
from .feed import FeedError, Snapshot, fetch_snapshot
from .fleet import Fleet
from .ingest import run_ingest
from .oracle import CoverageModel, PresenceMatrix, latest_calibrator, rolling_origin
from .rollup import rebuild_all
from .routes import parse_route
from .simulate import simulate_flights, simulate_network
from .staticdata import StaticReference, load_reference
from .store import Database

__all__ = ["main"]


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.load(data_dir=getattr(args, "data_dir", None))


def _fmt(value: int | float) -> str:
    return f"{int(value):,}"


# --------------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings(args)
    checks = doctor.run_checks(settings, include_weather=not args.no_weather)
    print(doctor.render(checks))
    return doctor.exit_code(checks)


# ---------------------------------------------------------------------------- sniff


@dataclass(slots=True)
class RouteStats:
    with_plan: int
    complete: int
    pairs: Counter[tuple[str, str]]
    departures: Counter[str]
    aircraft: Counter[str]


@dataclass(slots=True)
class CoverageStats:
    rows: list[tuple[str, int, int, int]]  # fir, traffic, enroute, terminal
    total_traffic: int
    unresolved: int
    covered_traffic: int
    staff_unknown: int


def _facility_histogram(snapshot: Snapshot) -> Counter[str]:
    return Counter(c.position.facility.value for c in snapshot.atc)


def _route_stats(snapshot: Snapshot) -> RouteStats:
    complete = [p for p in snapshot.pilots if p.flight_plan and p.flight_plan.is_complete]
    pairs: Counter[tuple[str, str]] = Counter()
    departures: Counter[str] = Counter()
    aircraft: Counter[str] = Counter()
    for pilot in complete:
        plan = pilot.flight_plan
        if plan is None:  # pragma: no cover - filtered above
            continue
        pairs[(plan.departure, plan.arrival)] += 1
        departures[plan.departure] += 1
        if plan.aircraft_short:
            aircraft[plan.aircraft_short] += 1
    return RouteStats(
        with_plan=len(snapshot.pilots_with_plan),
        complete=len(complete),
        pairs=pairs,
        departures=departures,
        aircraft=aircraft,
    )


def _coverage(snapshot: Snapshot, reference: StaticReference, top: int) -> CoverageStats:
    """Join traffic to staffing, at top-level FIR granularity.

    Sector-level modelling is P1/P2 work; at P0 the honest question is "is this
    aircraft under a FIR where an en-route controller is online", and the answer has
    to respect the parent/sub-sector hierarchy or it undercounts by an order of
    magnitude (which is exactly what the first version of this function did).
    """
    enroute: Counter[str] = Counter()
    terminal: Counter[str] = Counter()
    staff_unknown = 0
    for controller in snapshot.atc:
        code = reference.resolve_fir(controller.position.stem)
        if not code:
            staff_unknown += 1
            continue
        root = reference.root_fir(code)
        if controller.position.facility.value in ("CTR", "FSS"):
            enroute[root] += 1
        else:
            terminal[root] += 1

    covered_codes: set[str] = set()
    for code in enroute:
        covered_codes |= reference.boundary_codes_for_fir(code)

    traffic: Counter[str] = Counter()
    unresolved = 0
    covered_traffic = 0
    for pilot in snapshot.pilots:
        if not pilot.has_position:
            continue
        boundary = reference.boundary_at(pilot.latitude, pilot.longitude)  # type: ignore[arg-type]
        if boundary is None:
            unresolved += 1
            continue
        traffic[reference.root_fir(boundary.code)] += 1
        if boundary.code in covered_codes:
            covered_traffic += 1

    rows: list[tuple[str, int, int, int]] = []
    for code, count in traffic.most_common(top):
        rows.append((code, count, enroute.get(code, 0), terminal.get(code, 0)))

    return CoverageStats(
        rows=rows,
        total_traffic=sum(traffic.values()),
        unresolved=unresolved,
        covered_traffic=covered_traffic,
        staff_unknown=staff_unknown,
    )


def _sniff_payload(
    snapshot: Snapshot,
    histogram: Counter[str],
    routes: RouteStats,
    coverage: CoverageStats | None,
) -> dict[str, object]:
    return {
        "updated_at": snapshot.updated_at.isoformat() if snapshot.updated_at else None,
        "connected_clients": snapshot.connected_clients,
        "unique_users": snapshot.unique_users,
        "pilots": len(snapshot.pilots),
        "controllers_total": len(snapshot.controllers),
        "atc": len(snapshot.atc),
        "observers": len(snapshot.observers),
        "atis": len(snapshot.atis),
        "prefiled": snapshot.prefiled,
        "facilities": dict(histogram),
        "routes": {
            "with_plan": routes.with_plan,
            "complete": routes.complete,
            "top_pairs": [
                {"dep": dep, "arr": arr, "count": n}
                for (dep, arr), n in routes.pairs.most_common(8)
            ],
            "top_departures": [
                {"icao": icao, "count": n} for icao, n in routes.departures.most_common(8)
            ],
            "top_aircraft": [
                {"type": kind, "count": n} for kind, n in routes.aircraft.most_common(8)
            ],
        },
        "coverage": (
            {
                "total_traffic": coverage.total_traffic,
                "unresolved": coverage.unresolved,
                "covered_traffic": coverage.covered_traffic,
                "rows": [
                    {"fir": code, "traffic": n, "enroute": e, "terminal": t}
                    for code, n, e, t in coverage.rows
                ],
            }
            if coverage
            else None
        ),
    }


def cmd_sniff(args: argparse.Namespace) -> int:
    settings = _settings(args)
    try:
        snapshot = fetch_snapshot(settings)
    except FeedError as exc:
        print(f"feed unavailable: {exc}", file=sys.stderr)
        return 2

    histogram = _facility_histogram(snapshot)
    routes = _route_stats(snapshot)

    coverage: CoverageStats | None = None
    if args.coverage:
        try:
            reference = load_reference(settings)
        except RuntimeError as exc:
            print(f"static reference unavailable, skipping coverage: {exc}", file=sys.stderr)
        else:
            coverage = _coverage(snapshot, reference, args.top)

    if args.json:
        print(json.dumps(_sniff_payload(snapshot, histogram, routes, coverage), indent=2))
        return 0

    age = snapshot.age_seconds()
    stamp = snapshot.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ") if snapshot.updated_at else "unknown"
    age_text = f"{age:.0f}s" if age is not None else "?"
    print(f"VATSIM network - {stamp} (age {age_text})")
    print(
        f"  {_fmt(snapshot.connected_clients)} clients | "
        f"{_fmt(snapshot.unique_users)} unique users | "
        f"{_fmt(len(snapshot.pilots))} pilots | {_fmt(snapshot.prefiled)} prefiled"
    )
    print(
        f"  {_fmt(len(snapshot.atc))} ATC positions | "
        f"{_fmt(len(snapshot.observers))} observers filtered | "
        f"{_fmt(len(snapshot.atis))} ATIS"
    )
    if histogram:
        parts = " | ".join(f"{name} {count}" for name, count in sorted(histogram.items()))
        print(f"  by facility: {parts}")

    print("")
    print(
        f"  flight plans: {_fmt(routes.with_plan)} filed, "
        f"{_fmt(routes.complete)} complete (dep + arr + route)"
    )
    if routes.pairs:
        top_pairs = ", ".join(f"{d}->{a} ({n})" for (d, a), n in routes.pairs.most_common(5))
        print(f"  busiest city pairs: {top_pairs}")
    if routes.departures:
        top_deps = ", ".join(f"{d} ({n})" for d, n in routes.departures.most_common(5))
        print(f"  busiest departures: {top_deps}")
    if routes.aircraft:
        top_types = ", ".join(f"{t} ({n})" for t, n in routes.aircraft.most_common(5))
        print(f"  aircraft mix: {top_types}")

    if coverage is not None:
        total = coverage.total_traffic or 1
        share = 100.0 * coverage.covered_traffic / total
        print("")
        print(
            f"  traffic under en-route ATC: {share:.0f}% "
            f"({_fmt(coverage.covered_traffic)} of {_fmt(coverage.total_traffic)} positioned aircraft)"
        )
        if coverage.unresolved:
            print(f"  outside any known FIR: {_fmt(coverage.unresolved)}")
        print("")
        print(f"  {'FIR':<6} {'traffic':>8} {'CTR/FSS':>8} {'TWR/APP/GND':>12}")
        for code, count, enroute, terminal in coverage.rows:
            print(f"  {code:<6} {count:>8} {enroute:>8} {terminal:>12}")
        if coverage.staff_unknown:
            print(f"  ({coverage.staff_unknown} ATC positions could not be mapped to an FIR)")

    print("")
    print("  next: `skysherpa doctor` to health-check sources; PLAN.md for the roadmap")
    return 0


# --------------------------------------------------------------------- parsers


def cmd_parse_callsign(args: argparse.Namespace) -> int:
    position = parse_callsign(args.callsign, args.facility_code)
    print(f"raw       {position.raw}")
    print(f"stem      {position.stem}")
    print(f"key       {position.key}")
    print(f"facility  {position.facility.value}")
    print(f"observer  {position.is_observer}")
    print(f"atc       {position.is_atc}")
    print(f"sector    {position.sector or '-'}")
    print(f"airport   {position.airport or '-'}")
    return 0


def cmd_parse_route(args: argparse.Namespace) -> int:
    route = parse_route(args.route)
    for index, token in enumerate(route.tokens, start=1):
        print(f"{index:>3}  {token.text:<12} {token.kind.value}")
    print("")
    print(f"normalized  {route.normalized}")
    print(f"hash        {route.route_hash}")
    print(
        f"counts      fixes={route.n_fixes} airways={route.n_airways} "
        f"procedures={route.n_procedures} direct={route.has_direct}"
    )
    levels = route.levels_ft
    print(f"levels      {', '.join(f'FL{v // 100:03d}' for v in levels) if levels else '-'}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"skysherpa {__version__} | python {platform.python_version()} | {platform.platform()}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    settings = _settings(args)
    settings.ensure_dirs()
    print(f"data directory ready: {settings.data_dir}")
    print("next: `skysherpa doctor`")
    return 0


# ------------------------------------------------------------------- catalogue


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..."


def _reference_distances(aircraft) -> tuple[int, ...]:
    if aircraft.aircraft_class in ("general", "helicopter"):
        return (50, 150, 300)
    if aircraft.aircraft_class == "turboprop":
        return (100, 250, 500)
    return (200, 500, 1000)


def _print_aircraft(aircraft, catalog: AircraftCatalog) -> int:
    ceiling_fl = aircraft.ceiling_ft // 100
    print(f"{aircraft.icao}  {aircraft.name}")
    print("-" * 74)
    print(
        f"  {aircraft.manufacturer} | {aircraft.aircraft_class} | {aircraft.engine} | "
        f"wake {aircraft.wake} | {aircraft.engines} engine(s)"
    )
    print(f"  range      {aircraft.range_nm:>8,} nm")
    print(
        f"  cruise     {aircraft.cruise_tas_kt:>8,} kt at FL{aircraft.cruise_fl:03d}"
        f"   (ceiling FL{ceiling_fl:03d})"
    )
    print(f"  MTOW       {aircraft.mtow_kg:>8,} kg")
    if aircraft.min_runway_ft:
        print(f"  runway     {aircraft.min_runway_ft:>8,} ft")
    else:
        print("  runway          n/a   (helicopter)")
    print(f"  taxi       {aircraft.taxi_min:>8,} min   (ground allowance, both ends)")
    print(f"  confidence {aircraft.confidence:>8}")
    if aircraft.notes:
        print(f"  notes      {aircraft.notes}")

    print("")
    print("  estimated block time")
    for distance in _reference_distances(aircraft):
        minutes = aircraft.estimate_block_minutes(distance)
        print(f"    {distance:>6,} nm {minutes:>6,} min")

    variants = catalog.variants_for(aircraft.icao)
    print("")
    print(f"  add-ons ({len(variants)})")
    print(f"    {'ID':<26} {'PRODUCT':<34} {'DEVELOPER':<20} {'SIM':<7} FIDELITY")
    for variant in sorted(variants, key=lambda v: (v.sim, v.developer, v.name)):
        marker = " free" if variant.freeware else ""
        print(
            f"    {variant.id:<26} {_truncate(variant.name, 34):<34} "
            f"{_truncate(variant.developer, 20):<20} {variant.sim:<7} "
            f"{variant.fidelity}{marker}"
        )
    return 0


def cmd_aircraft(args: argparse.Namespace) -> int:
    catalog = load_catalog()

    if args.icao:
        aircraft = catalog.get(args.icao)
        if aircraft is None:
            print(f"unknown aircraft type: {args.icao}", file=sys.stderr)
            return 2
        return _print_aircraft(aircraft, catalog)

    results = catalog.filter(
        aircraft_class=args.aircraft_class,
        engine=args.engine,
        sim=args.sim,
        freeware=args.freeware,
        search=args.search,
        max_runway_ft=args.max_runway,
    )
    if not results:
        print("no aircraft match those filters", file=sys.stderr)
        return 2

    if args.json:
        payload = {
            "count": len(results),
            "catalog": {
                "types": len(catalog.types),
                "variants": len(catalog.variants),
            },
            "types": [
                {
                    "icao": t.icao,
                    "name": t.name,
                    "manufacturer": t.manufacturer,
                    "class": t.aircraft_class,
                    "engine": t.engine,
                    "wake": t.wake,
                    "range_nm": t.range_nm,
                    "cruise_tas_kt": t.cruise_tas_kt,
                    "cruise_fl": t.cruise_fl,
                    "variant_count": len(catalog.variants_for(t.icao)),
                }
                for t in results
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    classes = ", ".join(f"{name} {count}" for name, count in sorted(catalog.class_counts().items()))
    print(
        f"{len(catalog.types)} aircraft types | {len(catalog.variants)} add-on variants | {classes}"
    )
    print("")
    print(
        f"  {'ICAO':<5} {'AIRCRAFT':<34} {'CLASS':<11} {'WAKE':<5} {'RANGE':>6} {'TAS':>5} {'ADD-ON':>7}"
    )
    limit = args.limit if args.limit > 0 else len(results)
    for aircraft in results[:limit]:
        count = len(catalog.variants_for(aircraft.icao))
        print(
            f"  {aircraft.icao:<5} {_truncate(aircraft.name, 34):<34} "
            f"{aircraft.aircraft_class:<11} {aircraft.wake:<5} {aircraft.range_nm:>6,} "
            f"{aircraft.cruise_tas_kt:>5,} {count:>7}"
        )
    if limit < len(results):
        print("")
        print(f"  showing {limit} of {len(results)}; use --limit 0 for all")
    print("")
    print("  detail: skysherpa aircraft A20N     add to fleet: skysherpa fleet --add A20N")
    return 0


def cmd_fleet(args: argparse.Namespace) -> int:
    settings = _settings(args)
    catalog = load_catalog()
    fleet = Fleet(settings.data_dir / "fleet.json")

    if args.clear:
        fleet.clear()
        print("fleet cleared")
        return 0
    if args.remove:
        if fleet.remove(args.remove):
            print(f"removed {args.remove.upper()}")
            return 0
        print(f"{args.remove.upper()} is not in your fleet", file=sys.stderr)
        return 2
    if args.add:
        try:
            entry = fleet.add(args.add, catalog, variant_id=args.variant, label=args.label)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        variant = catalog.variant(entry.variant_id)
        print(f"added {entry.icao} as {variant.name if variant else entry.variant_id}")
        print("")

    entries = fleet.load()
    if not entries:
        print("your fleet is empty")
        print("  add one with: skysherpa fleet --add A20N --variant fenix-a320")
        print("  browse with : skysherpa aircraft --class airliner")
        return 0

    if args.json:
        payload = {
            "count": len(entries),
            "entries": [
                {"icao": e.icao, "variant_id": e.variant_id, "label": e.label} for e in entries
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"your fleet ({len(entries)})")
    print(f"  {'ICAO':<5} {'TYPE':<28} {'PRODUCT':<30} {'SIM':<7} {'BLOCK':>9}")
    total_range = 0
    for entry in entries:
        aircraft = catalog.get(entry.icao)
        variant = catalog.variant(entry.variant_id)
        if aircraft is None:
            print(f"  {entry.icao:<5} {'unknown type':<28} {entry.variant_id:<30}")
            continue
        reference = 150 if aircraft.aircraft_class in ("general", "helicopter") else 500
        minutes = aircraft.estimate_block_minutes(reference)
        total_range = max(total_range, aircraft.range_nm)
        print(
            f"  {entry.icao:<5} {_truncate(aircraft.name, 28):<28} "
            f"{_truncate(variant.name if variant else entry.variant_id, 30):<30} "
            f"{(variant.sim if variant else '?'):<7} {minutes:>4,} min @ {reference}nm"
        )
    print("")
    print(f"  longest range: {total_range:,} nm | remove with: skysherpa fleet --remove ICAO")
    return 0


# ------------------------------------------------------------------- persistence


def _raise_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _install_shutdown_handler() -> None:
    """Treat SIGTERM like Ctrl-C.

    systemd stops a unit with SIGTERM, and Python's default is to exit immediately -- which
    skips the flush that closes open controller sessions, finishes in-flight flight records
    and writes the publisher heartbeat. Raising KeyboardInterrupt reuses the cleanup paths
    that already exist for an interactive Ctrl-C instead of duplicating them.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    with contextlib.suppress(ValueError, OSError, AttributeError):
        # Best effort: some platforms and non-main threads refuse signal handlers.
        signal.signal(signal.SIGTERM, _raise_interrupt)


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = _settings(args)
    _install_shutdown_handler()
    return run_ingest(
        settings,
        once=args.once,
        minutes=args.minutes,
        interval_s=args.interval,
        quiet=args.quiet,
        max_polls=args.max_polls,
    )


def cmd_db(args: argparse.Namespace) -> int:
    settings = _settings(args)
    database = Database(settings.data_dir / "skysherpa.sqlite3")
    if not database.path.exists():
        print(f"no database yet at {database.path}")
        print("  create it with: skysherpa ingest --once")
        return 1
    database.initialize()
    stats = database.stats()

    if args.json:
        payload = {
            key: (value.isoformat() if hasattr(value, "isoformat") else value)
            for key, value in stats.items()
        }
        print(json.dumps(payload, indent=2))
        return 0

    size_mb = stats["bytes"] / 1_048_576
    first = stats["first_poll"].strftime("%Y-%m-%dT%H:%MZ") if stats["first_poll"] else "-"
    last = stats["last_poll"].strftime("%Y-%m-%dT%H:%MZ") if stats["last_poll"] else "-"
    span = (
        f"{stats['span_hours']:.1f} h"
        if stats["span_hours"]
        else ("single poll" if stats["snapshots"] else "empty")
    )
    print(f"database  {stats['path']}")
    print(f"          {size_mb:.1f} MB | schema v{stats['schema_version']}")
    print("")
    print(f"  {'snapshots':<14}{stats['snapshots']:>9,}   {first} -> {last} ({span})")
    print(f"  {'positions':<14}{stats['positions']:>9,}")
    print(f"  {'sessions':<14}{stats['sessions']:>9,}   {stats['atc_hours']:.1f} h of ATC observed")
    print(f"  {'flights':<14}{stats['flights']:>9,}   mean block {stats['block_min_avg']:.0f} min")
    print(f"  {'track points':<14}{stats['track_points']:>9,}")
    print(f"  {'gaps':<14}{stats['gaps']:>9,}")
    if stats["snapshots"] == 0:
        print("")
        print("  nothing collected yet: run `skysherpa ingest --once` to take a first poll")
    return 0


# ---------------------------------------------------------------- forecasting


HEATMAP = " .:-=+*#%@"


def _open_db(settings: Settings) -> Database:
    database = Database(settings.data_dir / "skysherpa.sqlite3")
    if not database.path.exists():
        raise FileNotFoundError(
            f"no database at {database.path}; run `skysherpa ingest --once` or "
            "`skysherpa simulate` first"
        )
    database.initialize()
    return database


def cmd_rollup(args: argparse.Namespace) -> int:
    settings = _settings(args)
    database = _open_db(settings)
    result = rebuild_all(database)
    print(
        f"rolled up: {result.slots:,} presence slots | {result.pairs:,} city pairs | "
        f"{result.airports:,} airport-hours | {result.variants:,} route variants"
    )
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    settings = _settings(args)
    settings.ensure_dirs()
    path = settings.data_dir / "skysherpa.sqlite3"
    if path.exists():
        if not args.force:
            print(f"{path} already exists; use --force to replace it", file=sys.stderr)
            return 2
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                candidate.unlink()

    database = Database(path)
    database.initialize()
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    network = simulate_network(database, days=args.days, seed=args.seed, end=end)
    flights = simulate_flights(database, days=args.days, seed=args.seed + 1, end=end)
    result = rebuild_all(database)
    print(
        f"synthetic network: {network.positions} positions | {network.sessions:,} sessions | "
        f"{flights:,} flights over {args.days} days"
    )
    print(
        f"rolled up: {result.slots:,} presence slots | {result.pairs:,} city pairs | "
        f"{result.airports:,} airport-hours"
    )
    print("")
    print(f"  try: skysherpa coverage --data-dir {settings.data_dir} --fir EGTT")
    return 0


def _select_positions(database: Database, args: argparse.Namespace) -> list[str]:
    if args.position:
        return [args.position.upper()]
    clauses = ["1=1"]
    params: list[str] = []
    if args.fir:
        clauses.append("fir_code = ?")
        params.append(args.fir.upper())
    if args.airport:
        clauses.append("airport_icao = ?")
        params.append(args.airport.upper())
    if args.facility:
        facilities = [item.strip().upper() for item in args.facility.split(",") if item.strip()]
        clauses.append(f"facility IN ({','.join('?' for _ in facilities)})")
        params.extend(facilities)
    query = (
        "SELECT position_key FROM position WHERE "
        + " AND ".join(clauses)
        + " ORDER BY position_key"
    )
    return [str(row["position_key"]) for row in database.query(query, params)]


def cmd_coverage(args: argparse.Namespace) -> int:
    settings = _settings(args)
    try:
        database = _open_db(settings)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    matrix = PresenceMatrix.from_db(database)
    if not matrix.days:
        print("no presence data yet; run `skysherpa rollup` first", file=sys.stderr)
        return 1

    model = CoverageModel.fit(matrix, reference_date=date.today())
    calibrator = latest_calibrator(matrix)
    if calibrator.knots:
        model.calibrate_with(calibrator)
    positions = _select_positions(database, args)
    if not positions:
        print("no positions matched; try --fir EGTT or --airport EGLL", file=sys.stderr)
        return 1

    scoring = {position: model.mean_probability(position) for position in positions}
    positions = sorted(positions, key=lambda item: scoring[item], reverse=True)[: args.top]

    grid = [[0.0] * 24 for _ in range(7)]
    for weekday in range(7):
        for hour in range(24):
            total = 0.0
            for position in positions:
                p_hour = (
                    sum(
                        model.predict(position, weekday, hour * 4 + part, with_interval=False).p
                        for part in range(4)
                    )
                    / 4.0
                )
                total += p_hour
            grid[weekday][hour] = total / len(positions)

    if args.json:
        print(
            json.dumps(
                {
                    "positions": positions,
                    "mean_probability": {p: round(scoring[p], 3) for p in positions},
                    "grid": [[round(value, 3) for value in row] for row in grid],
                    "days_observed": len({d for rows in matrix.days.values() for d, _, _ in rows}),
                },
                indent=2,
            )
        )
        return 0

    label = args.fir or args.airport or args.position or "all positions"
    print(f"staffing forecast - {label} - {len(positions)} position(s)")
    print(
        f"  trained on {matrix.observations():,} slot observations, "
        f"{len({d for rows in matrix.days.values() for d, _, _ in rows})} days, "
        f"half-life {model.params.half_life_days:.0f}d"
    )
    print("  legend: share of these positions expected online")
    print("          space=0%  .=10%  :=20%  -=30%  ==40%  +=50%  *=60%  #=70%  %=80%  @=90%+")
    print("")
    print("       " + " ".join(f"{hour:02d}" for hour in range(24)))
    for weekday in range(7):
        name = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[weekday]
        cells = "  ".join(HEATMAP[min(9, int(value * 10))] for value in grid[weekday])
        print(f"  {name}  {cells}")
    print("")

    busiest = sorted(positions, key=lambda item: scoring[item], reverse=True)[:6]
    for position in busiest:
        peak_hour = max(
            range(24),
            key=lambda hour: sum(
                model.predict(position, weekday, hour * 4 + part, with_interval=False).p
                for weekday in range(7)
                for part in range(4)
            ),
        )
        print(
            f"  {position:<12} mean {scoring[position] * 100:5.1f}%   peak {peak_hour:02d}:00 UTC"
        )
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    settings = _settings(args)
    try:
        database = _open_db(settings)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    matrix = PresenceMatrix.from_db(database)
    report = rolling_origin(matrix, folds=args.folds, horizon_days=args.horizon)
    if args.json:
        payload = report.as_dict()
        payload["notes"] = report.notes
        payload["gates"] = [
            {"name": name, "passed": passed, "value": value}
            for name, passed, value in report.gates()
        ]
        print(json.dumps(payload, indent=2))
        return 0

    if report.predictions == 0:
        print("not enough history to backtest yet")
        for note in report.notes:
            print(f"  {note}")
        return 1

    print(f"rolling-origin backtest - {report.folds} folds x {args.horizon} days")
    print(f"  {report.predictions:,} held-out predictions across {report.positions} positions")
    print("")
    print(f"  {'brier':<22}{report.brier:.4f}   baseline {report.brier_baseline:.4f}")
    print(f"  {'brier skill':<22}{report.brier_skill:.4f}")
    print(
        f"  {'backed cells':<22}{report.backed_share:.0%} of predictions"
        f"   skill there {report.brier_skill_backed:.4f}"
    )
    print(f"  {'auc':<22}{report.auc:.4f}")
    print(f"  {'calibration error':<22}{report.ece:.4f}")
    print(f"  {'worst bin (1%+)':<22}{report.reliability_max_dev:.4f}")
    print(
        f"  {'interval coverage':<22}{report.interval_coverage:.4f}"
        f"   over {report.cells_checked:,} cells"
    )
    print("")
    failures = 0
    for name, passed, value in report.gates():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}: {value}")
        failures += 0 if passed else 1
    if args.bins:
        print("")
        print(f"  {'predicted':>10} {'observed':>9} {'n':>8}")
        for mean_p, observed, count in report.bins:
            print(f"  {mean_p:>10.3f} {observed:>9.3f} {count:>8,}")
    for note in report.notes:
        print(f"  note: {note}")
    return 1 if failures else 0


# ------------------------------------------------------------------- publishing


def _publish_once(args: argparse.Namespace, settings: Settings) -> int:
    try:
        database = _open_db(settings)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    reference = None
    try:
        reference = load_reference(settings)
    except RuntimeError as exc:
        print(
            f"warning: no static reference ({exc}); airports and map will be empty", file=sys.stderr
        )

    # Publishing without rolling up first is the classic footgun: the bundle would be
    # built from stale aggregates. Do it here unless explicitly told not to.
    if not args.no_rollup:
        rolled = rebuild_all(database, reference)
        print(
            f"rolled up {rolled.slots:,} slots | {rolled.pairs:,} pairs | "
            f"{rolled.airports:,} airport-hours"
        )

    out = Path(args.out) if args.out else settings.root / "dist"
    try:
        result = build_bundle(
            database,
            out_dir=out,
            reference=reference,
            max_pairs=args.max_pairs,
            write_web_assets=not args.no_assets,
            calibrate=not args.no_calibrate,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        database.close()

    # One line per cycle in watch mode; the full inventory when a human asked once.
    print(result.summary(detail=not getattr(args, "watch", False) or args.verbose))

    # A bundle that does not add up is worse than no bundle: the site will load most of it
    # and fail somewhere unhelpful. Check it here while the operator is still watching.
    from .verify import verify_bundle

    problems = verify_bundle(out, check_assets=not args.no_assets)
    if problems:
        print("", file=sys.stderr)
        print(f"bundle is incoherent ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("")
    print(f"  serve:  python -m http.server 8080 --directory {out}")
    print("  open:   http://127.0.0.1:8080/")
    return 0


def _write_heartbeat(settings: Settings, args: argparse.Namespace, phase: str, **extra) -> None:
    """Record what the watch loop is doing, on disk rather than only on stdout.

    A stalled loop can lose its console, and a hang *during* a cycle is indistinguishable
    from a dead process if only successes are recorded. Writing "started" before the work
    and "ok"/"hung" after it makes the two cases tell apart.
    """
    try:
        out_dir = Path(args.out) if args.out else settings.root / "dist"
        out_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {"at": datetime.now(UTC).isoformat(), "phase": phase}
        payload.update(extra)
        (out_dir / ".publisher.json").write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def cmd_publish(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if not args.watch:
        return _publish_once(args, settings)
    _install_shutdown_handler()

    interval = max(60, args.interval)
    # A cycle that hangs produces no output, no error, and a website that quietly goes
    # stale: exactly what happened once for 54 minutes while `status` reported the
    # collector healthy. Run each cycle in a worker and abandon it if it overruns.
    budget = max(60, args.timeout)
    print(f"publishing every {interval // 60} min; cycle timeout {budget}s; ctrl-c to stop")
    while True:
        try:
            started = time.monotonic()
            _write_heartbeat(settings, args, "started", interval=interval)
            worker = threading.Thread(target=_publish_once, args=(args, settings), daemon=True)
            worker.start()
            worker.join(timeout=budget)
            if worker.is_alive():
                print(
                    f"ALARM [{datetime.now(UTC).strftime('%H:%M:%SZ')}] a publish cycle "
                    f"exceeded {budget}s and was abandoned; the site is not being updated",
                    file=sys.stderr,
                    flush=True,
                )
                _write_heartbeat(settings, args, "hung", interval=interval)
            else:
                elapsed = time.monotonic() - started
                print(
                    f"[{datetime.now(UTC).strftime('%H:%M:%SZ')}] published in {elapsed:.1f}s",
                    flush=True,
                )
                _write_heartbeat(settings, args, "ok", seconds=round(elapsed, 1), interval=interval)
        except KeyboardInterrupt:
            print("stopped")
            return 0
        except Exception as exc:
            print(f"publish failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("stopped")
            return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    try:
        database = _open_db(settings)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    stats = database.stats()
    matrix = PresenceMatrix.from_db(database)
    days = len({day for rows in matrix.days.values() for day, _, _ in rows})
    observations = matrix.observations()
    pairs = int(database.scalar("SELECT COUNT(*) FROM pair_stat") or 0)
    first = stats["first_poll"].strftime("%Y-%m-%d") if stats["first_poll"] else "-"
    last = stats["last_poll"].strftime("%Y-%m-%d") if stats["last_poll"] else "-"

    # The first question on a 24/7 box is "is my collector still alive?". A database with a
    # day of history and a dead ingestor looks exactly like a healthy one until you notice
    # the dates.
    if stats["last_poll"] is None:
        collector_age = None
        collector = "no polls yet"
    else:
        collector_age = (datetime.now(UTC) - stats["last_poll"]).total_seconds()
        if collector_age < 300:
            collector = f"last poll {collector_age:.0f}s ago (alive)"
        else:
            collector = f"last poll {collector_age / 60:.0f} min ago (STALLED)"

    checks = [
        ("planning", pairs > 0, f"{pairs:,} city pairs", 0),
        ("forecast", days >= 14, f"{days} day(s) of presence", max(0, 14 - days)),
        ("backtest", days >= 35, f"{days} day(s) of presence", max(0, 35 - days)),
        ("calibration", days >= 14, f"{days} day(s) for a holdout", max(0, 14 - days)),
    ]

    if args.json:
        print(
            json.dumps(
                {
                    "database": stats["path"],
                    "bytes": stats["bytes"],
                    "first_poll": first,
                    "last_poll": last,
                    "snapshots": stats["snapshots"],
                    "gaps": stats["gaps"],
                    "positions": stats["positions"],
                    "atc_hours": round(stats["atc_hours"], 2),
                    "flights": stats["flights"],
                    "presence_slots": observations,
                    "days_observed": days,
                    "city_pairs": pairs,
                    "collector": {
                        "last_poll": last,
                        "age_seconds": collector_age,
                        "alive": bool(collector_age is not None and collector_age < 300),
                    },
                    "readiness": [
                        {"name": name, "ready": ready, "detail": detail, "days_remaining": wait}
                        for name, ready, detail, wait in checks
                    ],
                },
                indent=2,
            )
        )
        return 0

    print(f"database   {stats['path']}")
    print(
        f"           {stats['bytes'] / 1_048_576:.1f} MB | {stats['snapshots']:,} snapshots | "
        f"{stats['gaps']:,} gaps"
    )
    print(f"history    {first} to {last} ({stats['span_hours']:.1f} h)")
    print(
        f"observed   {stats['positions']:,} positions | {stats['atc_hours']:.0f} h of ATC | "
        f"{stats['flights']:,} flights"
    )
    print(f"matrix     {observations:,} presence slots over {days} day(s)")
    print(f"collector  {collector}")

    # The publisher writes a heartbeat per cycle; without this line its death is invisible,
    # because everything downstream of it keeps looking plausible.
    publisher = "no heartbeat yet (publish --watch writes one per cycle)"
    site_root = Path(args.site) if getattr(args, "site", None) else settings.root
    beats: list[tuple[datetime, str, float, str]] = []
    for site in ("dist", "dist-real"):
        try:
            payload = json.loads((site_root / site / ".publisher.json").read_text(encoding="utf-8"))
            moment = datetime.fromisoformat(payload["at"])
            interval_s = float(payload.get("interval") or 900)
            beats.append(
                (moment, site, max(600.0, interval_s * 2), str(payload.get("phase") or "ok"))
            )
        except (OSError, ValueError, KeyError):
            continue
    publisher_age: float | None = None
    publisher_limit = 900.0
    publisher_phase = "ok"
    if beats:
        moment, site, publisher_limit, publisher_phase = max(beats)
        publisher_age = (datetime.now(UTC) - moment).total_seconds()
        where = (
            f"{publisher_age:.0f}s ago"
            if publisher_age < 90
            else f"{publisher_age / 60:.0f} min ago"
        )
        if publisher_phase == "hung":
            verdict = "HUNG, cycle abandoned"
        elif publisher_phase == "started" and publisher_age > publisher_limit:
            verdict = "HUNG, a cycle started and never finished"
        elif publisher_age > publisher_limit:
            verdict = "STALLED"
        else:
            verdict = "alive"
        publisher = f"{site}: last cycle {where} ({verdict})"
    print(f"publisher  {publisher}")
    print("")
    print("readiness")
    # "Needs 33 more days" is hard to plan around; a date is not.
    first_day = stats["first_poll"].date() if stats["first_poll"] else None
    for name, ready, detail, wait in checks:
        marker = "[x]" if ready else "[ ]"
        suffix = ""
        if not ready:
            suffix = f"  (needs {wait} more day{'s' if wait != 1 else ''}"
            if first_day is not None:
                suffix += f", around {(first_day + timedelta(days=days + wait)).isoformat()}"
            suffix += ")"
        print(f"  {marker} {name:<12} {detail}{suffix}")
    print("")
    blocking = [name for name, ready, _, _ in checks if not ready]
    if blocking:
        print(f"  next: keep the collector running; waiting on {', '.join(blocking)}")
    else:
        print("  next: ready to backtest and calibrate on real data")

    # Usable from a scheduler: exit non-zero when either long-running process is stale.
    if getattr(args, "check", False):
        stale: list[str] = []
        if collector_age is None or collector_age > 300:
            stale.append(f"collector ({collector})")
        if publisher_age is None:
            stale.append("publisher (no heartbeat; is `publish --watch` running?)")
        elif publisher_age > publisher_limit or publisher_phase == "hung":
            stale.append(f"publisher ({publisher})")
        database.close()
        if stale:
            print("", file=sys.stderr)
            print(f"unhealthy: {'; '.join(stale)}", file=sys.stderr)
            return 1
        print("healthy: collector and publisher are both current")
        return 0

    database.close()
    return 0


def cmd_rehearse(args: argparse.Namespace) -> int:
    from .rehearse import default_reference, run_rehearsal

    reference = default_reference()
    report = run_rehearsal(
        days=args.days,
        airports=args.airports,
        flights_per_day=args.flights_per_day,
        seed=args.seed,
        reference=reference,
        out_dir=Path(args.out) if args.out else None,
    )
    print(report.summary())
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    from .prune import prune_history

    settings = _settings(args)
    try:
        database = _open_db(settings)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        result = prune_history(database, keep_days=args.keep_days, dry_run=args.dry_run)
    finally:
        database.close()
    suffix = " (dry run)" if args.dry_run else ""
    print(result.summary() + suffix)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from .verify import verify_bundle

    settings = _settings(args)
    out = Path(args.out) if args.out else settings.root / "dist"
    problems = verify_bundle(out)
    if problems:
        print(f"{len(problems)} problem(s) in {out}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"{out}: bundle is coherent")
    return 0


# ------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", help="override the data directory")

    parser = argparse.ArgumentParser(
        prog="skysherpa",
        description="A dispatcher for the VATSIM network.",
    )
    parser.add_argument("--version", action="version", version=f"skysherpa {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", parents=[common], help="health-check every upstream source")
    p_doctor.add_argument("--no-weather", action="store_true", help="skip METAR/TAF checks")
    p_doctor.set_defaults(func=cmd_doctor)

    p_sniff = sub.add_parser("sniff", parents=[common], help="live network read-out")
    p_sniff.add_argument("--top", type=int, default=12, help="rows in the coverage table")
    p_sniff.add_argument(
        "--coverage",
        dest="coverage",
        action="store_true",
        default=True,
        help="join traffic to FIR polygons (default: on)",
    )
    p_sniff.add_argument(
        "--no-coverage", dest="coverage", action="store_false", help="skip the FIR join"
    )
    p_sniff.add_argument("--json", action="store_true", help="machine-readable output")
    p_sniff.set_defaults(func=cmd_sniff)

    p_pos = sub.add_parser("parse-callsign", parents=[common], help="parse an ATC callsign")
    p_pos.add_argument("callsign")
    p_pos.add_argument("--facility-code", type=int, default=None, help="feed facility code (0-6)")
    p_pos.set_defaults(func=cmd_parse_callsign)

    p_route = sub.add_parser("parse-route", parents=[common], help="tokenise a filed route")
    p_route.add_argument("route")
    p_route.set_defaults(func=cmd_parse_route)

    p_aircraft = sub.add_parser("aircraft", parents=[common], help="browse the aircraft catalogue")
    p_aircraft.add_argument("icao", nargs="?", help="show one type in detail")
    p_aircraft.add_argument("--class", dest="aircraft_class", choices=sorted(CLASSES))
    p_aircraft.add_argument("--engine", choices=sorted(ENGINES))
    p_aircraft.add_argument("--sim", choices=sorted(SIMS - {"any"}))
    p_aircraft.add_argument("--freeware", action="store_true", default=None)
    p_aircraft.add_argument("--search", help="match on ICAO, name, or manufacturer")
    p_aircraft.add_argument("--max-runway", type=int, dest="max_runway", metavar="FT")
    p_aircraft.add_argument("--limit", type=int, default=40, help="0 = all")
    p_aircraft.add_argument("--json", action="store_true")
    p_aircraft.set_defaults(func=cmd_aircraft)

    p_fleet = sub.add_parser("fleet", parents=[common], help="manage your aircraft")
    p_fleet.add_argument("--add", metavar="ICAO", help="add a type to your fleet")
    p_fleet.add_argument("--variant", help="specific add-on (see `skysherpa aircraft ICAO`)")
    p_fleet.add_argument("--label", default="", help="your own name for it")
    p_fleet.add_argument("--remove", metavar="ICAO")
    p_fleet.add_argument("--clear", action="store_true")
    p_fleet.add_argument("--json", action="store_true")
    p_fleet.set_defaults(func=cmd_fleet)

    p_ingest = sub.add_parser(
        "ingest", parents=[common], help="poll the live feed into the database"
    )
    p_ingest.add_argument("--once", action="store_true", help="one poll, then exit")
    p_ingest.add_argument(
        "--minutes", type=float, default=0.0, help="run for N minutes (0 = forever)"
    )
    p_ingest.add_argument("--interval", type=int, default=None, help="seconds between polls")
    p_ingest.add_argument("--max-polls", type=int, default=None, help="stop after N polls")
    p_ingest.add_argument("--quiet", action="store_true")
    p_ingest.set_defaults(func=cmd_ingest)

    p_db = sub.add_parser("db", parents=[common], help="database status")
    p_db.add_argument("--json", action="store_true")
    p_db.set_defaults(func=cmd_db)

    p_status = sub.add_parser(
        "status", parents=[common], help="readiness: history, matrix, and what still blocks"
    )
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when the collector or publisher is stale (for a scheduler)",
    )
    p_status.add_argument(
        "--site", help="directory containing dist/ and dist-real/ (default: repo root)"
    )
    p_status.set_defaults(func=cmd_status)

    p_rehearse = sub.add_parser(
        "rehearse", parents=[common], help="time the pipeline against a synthetic long history"
    )
    p_rehearse.add_argument("--days", type=int, default=120)
    p_rehearse.add_argument("--airports", type=int, default=40)
    p_rehearse.add_argument("--flights-per-day", type=int, default=150)
    p_rehearse.add_argument("--seed", type=int, default=7)
    p_rehearse.add_argument("--out", help="keep the rehearsal database here")
    p_rehearse.set_defaults(func=cmd_rehearse)

    p_prune = sub.add_parser(
        "prune", parents=[common], help="drop raw flights older than N days, keeping aggregates"
    )
    p_prune.add_argument("--keep-days", type=int, default=45)
    p_prune.add_argument("--dry-run", action="store_true")
    p_prune.set_defaults(func=cmd_prune)

    p_verify = sub.add_parser(
        "verify", parents=[common], help="check that a published bundle is internally coherent"
    )
    p_verify.add_argument("--out", help="bundle directory (default: dist)")
    p_verify.set_defaults(func=cmd_verify)

    p_rollup = sub.add_parser(
        "rollup", parents=[common], help="rebuild presence and route aggregates"
    )
    p_rollup.set_defaults(func=cmd_rollup)

    p_sim = sub.add_parser(
        "simulate", parents=[common], help="build a synthetic network for testing"
    )
    p_sim.add_argument("--days", type=int, default=56)
    p_sim.add_argument("--seed", type=int, default=11)
    p_sim.add_argument("--force", action="store_true", help="replace an existing database")
    p_sim.set_defaults(func=cmd_simulate)

    p_cov = sub.add_parser(
        "coverage", parents=[common], help="forecast coverage as a weekly heatmap"
    )
    p_cov.add_argument("--fir", help="e.g. EGTT")
    p_cov.add_argument("--airport", help="e.g. EGLL")
    p_cov.add_argument("--position", help="a single position key")
    p_cov.add_argument("--facility", help="comma separated, e.g. CTR,FSS")
    p_cov.add_argument("--top", type=int, default=100)
    p_cov.add_argument("--json", action="store_true")
    p_cov.set_defaults(func=cmd_coverage)

    p_bt = sub.add_parser("backtest", parents=[common], help="rolling-origin forecast evaluation")
    p_bt.add_argument("--folds", type=int, default=4)
    p_bt.add_argument("--horizon", type=int, default=7)
    p_bt.add_argument("--bins", action="store_true", help="print the reliability diagram")
    p_bt.add_argument("--json", action="store_true")
    p_bt.set_defaults(func=cmd_backtest)

    p_pub = sub.add_parser(
        "publish", parents=[common], help="build the static web bundle (forecast + geometry)"
    )
    p_pub.add_argument("--out", help="output directory (default: ./dist)")
    p_pub.add_argument("--max-pairs", type=int, default=20_000)
    p_pub.add_argument("--no-assets", action="store_true", help="skip copying web/ into dist")
    p_pub.add_argument(
        "--no-calibrate", action="store_true", help="export raw probabilities (diagnostics)"
    )
    p_pub.add_argument(
        "--no-rollup", action="store_true", help="skip rebuilding aggregates before export"
    )
    p_pub.add_argument(
        "--watch", action="store_true", help="republish on an interval (keeps the site fresh)"
    )
    p_pub.add_argument(
        "--interval", type=int, default=900, help="seconds between publishes in --watch mode"
    )
    p_pub.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="abandon a publish cycle that runs longer than this in --watch mode",
    )
    p_pub.add_argument(
        "--verbose",
        action="store_true",
        help="print the full file inventory on every watch cycle",
    )
    p_pub.set_defaults(func=cmd_publish)

    sub.add_parser("version", parents=[common], help="print version").set_defaults(func=cmd_version)
    sub.add_parser("init", parents=[common], help="create the data directory").set_defaults(
        func=cmd_init
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:  # pragma: no cover - piping into `head`
        return 0
