"""Command line interface.

P0 commands are diagnostic, but not toys: `sniff --coverage` is the first honest slice
of the product -- it joins live traffic to FIR boundaries and shows the gap between
where the aircraft are and where the controllers are.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from collections import Counter
from dataclasses import dataclass

from . import __version__, doctor
from .aircraft import CLASSES, ENGINES, SIMS, AircraftCatalog, load_catalog
from .callsign import parse as parse_callsign
from .config import Settings
from .feed import FeedError, Snapshot, fetch_snapshot
from .fleet import Fleet
from .routes import parse_route
from .staticdata import StaticReference, load_reference

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
                {"dep": dep, "arr": arr, "count": n} for (dep, arr), n in routes.pairs.most_common(8)
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
    print(
        f"  taxi       {aircraft.taxi_min:>8,} min"
        f"   (ground allowance, both ends)"
    )
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

    classes = ", ".join(
        f"{name} {count}" for name, count in sorted(catalog.class_counts().items())
    )
    print(
        f"{len(catalog.types)} aircraft types | {len(catalog.variants)} add-on variants | {classes}"
    )
    print("")
    print(f"  {'ICAO':<5} {'AIRCRAFT':<34} {'CLASS':<11} {'WAKE':<5} {'RANGE':>6} {'TAS':>5} {'ADD-ON':>7}")
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

    p_aircraft = sub.add_parser(
        "aircraft", parents=[common], help="browse the aircraft catalogue"
    )
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

    sub.add_parser("version", parents=[common], help="print version").set_defaults(
        func=cmd_version
    )
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
