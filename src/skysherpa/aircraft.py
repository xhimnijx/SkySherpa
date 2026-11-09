"""The aircraft catalogue: what the user can fly, and how fast it gets there.

There is a deliberate choice here: this is a *shipped* dataset, not a scan of the
user's simulator install. Scanning ``Community`` or ``Custom Scenery`` is fragile,
OS-specific, and fails the moment someone uses a Marketplace add-on or a cloud
streaming install. A curated catalogue works on every machine, diffs in git, and --
crucially -- carries the numbers a dispatcher needs (range, runway, climb rate),
which an installation scan cannot provide.

Two layers:

* :class:`AircraftType` -- the airframe. ICAO type designator, performance, limits.
  This is what the optimiser reasons about.
* :class:`AircraftVariant` -- the product the user actually owns (Fenix A320,
  Asobo A320neo, FlyByWire A32NX). Same airframe, different fidelity, different sim.
  This is what the user picks in a menu.

Block time is estimated from a real climb/cruise/descent profile rather than a
distance-per-hour fudge, because "can I do this in three hours" is the question the
whole product answers.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "AircraftCatalog",
    "AircraftType",
    "AircraftVariant",
    "CLASSES",
    "ENGINES",
    "FIDELITY",
    "SIMS",
    "WAKES",
    "estimate_block_minutes",
    "load_catalog",
]

DATA_DIR = Path(__file__).resolve().parent / "data"

CLASSES = frozenset(
    {"airliner", "regional", "business", "general", "helicopter", "military", "vintage"}
)
ENGINES = frozenset({"jet", "turboprop", "piston", "turboshaft"})
WAKES = frozenset({"L", "M", "H", "J"})
SIMS = frozenset({"msfs", "xplane", "p3d", "fsx", "any"})
FIDELITY = frozenset({"study", "high", "standard", "default", "light"})


@dataclass(frozen=True, slots=True)
class AircraftType:
    """An airframe, with the numbers a dispatcher needs."""

    icao: str
    name: str
    manufacturer: str
    aircraft_class: str
    engine: str
    wake: str
    engines: int
    range_nm: int
    cruise_tas_kt: int
    cruise_fl: int
    ceiling_ft: int
    mtow_kg: int
    min_runway_ft: int
    climb_fpm: int
    descent_fpm: int
    taxi_min: int
    confidence: str
    notes: str = ""

    @property
    def cruise_alt_ft(self) -> int:
        return self.cruise_fl * 100

    def estimate_block_minutes(
        self,
        distance_nm: float,
        *,
        cruise_alt_ft: int | None = None,
        wind_kt: float = 0.0,
        approach_min: float = 3.0,
    ) -> int:
        return estimate_block_minutes(
            self,
            distance_nm,
            cruise_alt_ft=cruise_alt_ft,
            wind_kt=wind_kt,
            approach_min=approach_min,
        )


@dataclass(frozen=True, slots=True)
class AircraftVariant:
    """A specific product the user may own."""

    id: str
    icao: str
    name: str
    developer: str
    sim: str
    fidelity: str
    freeware: bool
    notes: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} ({self.developer})"


def estimate_block_minutes(
    aircraft: AircraftType,
    distance_nm: float,
    *,
    cruise_alt_ft: int | None = None,
    wind_kt: float = 0.0,
    approach_min: float = 3.0,
) -> int:
    """Estimated gate-to-gate time in minutes.

    A three-segment profile: climb at the type's climb rate, cruise at TAS, descend at
    the descent rate, plus a fixed ground and approach allowance. For short sectors the
    climb and descent segments are scaled to the distance available, so a 30 nm hop does
    not pretend to reach FL370.

    Accuracy is roughly +/-10% against real block times, which is the right order for a
    planning tool: it should never be the reason a user misses a slot, and it should be
    close enough that a 3-hour request returns 3-hour flights.
    """
    if distance_nm < 0:
        raise ValueError("distance_nm cannot be negative")

    target_alt = min(cruise_alt_ft or aircraft.cruise_alt_ft, aircraft.ceiling_ft)
    target_alt = max(0, target_alt)
    ground_allowance = aircraft.taxi_min + approach_min

    if distance_nm == 0:
        return max(1, round(ground_allowance))

    climb_rate = max(100, aircraft.climb_fpm)
    descent_rate = max(100, aircraft.descent_fpm)
    climb_speed = max(110.0, 0.62 * aircraft.cruise_tas_kt)
    descent_speed = max(110.0, 0.80 * aircraft.cruise_tas_kt)

    climb_min = target_alt / climb_rate
    descent_min = target_alt / descent_rate
    climb_dist = climb_min / 60.0 * climb_speed
    descent_dist = descent_min / 60.0 * descent_speed

    # A sector too short to complete the profile gets a proportional slice of it: the
    # aircraft simply levels off lower, and both time and distance shrink together.
    profile_dist = climb_dist + descent_dist
    if profile_dist > 0 and distance_nm < profile_dist:
        fraction = distance_nm / profile_dist
        climb_min *= fraction
        descent_min *= fraction
        climb_dist *= fraction
        descent_dist *= fraction

    cruise_dist = max(0.0, distance_nm - climb_dist - descent_dist)
    ground_speed = max(60.0, aircraft.cruise_tas_kt + wind_kt)
    cruise_min = cruise_dist / ground_speed * 60.0

    return max(1, round(ground_allowance + climb_min + cruise_min + descent_min))


@dataclass(frozen=True, slots=True)
class AircraftCatalog:
    types: dict[str, AircraftType]
    variants: tuple[AircraftVariant, ...]

    def get(self, icao: str) -> AircraftType | None:
        return self.types.get(icao.strip().upper())

    def variants_for(self, icao: str) -> list[AircraftVariant]:
        key = icao.strip().upper()
        return [v for v in self.variants if v.icao == key]

    def variant(self, variant_id: str) -> AircraftVariant | None:
        for candidate in self.variants:
            if candidate.id == variant_id:
                return candidate
        return None

    def search(self, text: str) -> list[AircraftType]:
        needle = text.strip().lower()
        if not needle:
            return sorted(self.types.values(), key=lambda t: t.icao)
        return sorted(
            (
                t
                for t in self.types.values()
                if needle in t.icao.lower()
                or needle in t.name.lower()
                or needle in t.manufacturer.lower()
            ),
            key=lambda t: t.icao,
        )

    def filter(
        self,
        *,
        aircraft_class: str | None = None,
        engine: str | None = None,
        sim: str | None = None,
        freeware: bool | None = None,
        search: str | None = None,
        min_range_nm: int | None = None,
        max_runway_ft: int | None = None,
        min_range_for: float | None = None,
    ) -> list[AircraftType]:
        results = self.search(search or "")
        if aircraft_class:
            results = [t for t in results if t.aircraft_class == aircraft_class]
        if engine:
            results = [t for t in results if t.engine == engine]
        if min_range_nm is not None:
            results = [t for t in results if t.range_nm >= min_range_nm]
        if max_runway_ft is not None:
            results = [t for t in results if 0 < t.min_runway_ft <= max_runway_ft]
        if min_range_for is not None:
            results = [t for t in results if t.range_nm >= min_range_for]
        if sim or freeware is not None:
            results = [t for t in results if self._has_variant(t.icao, sim=sim, freeware=freeware)]
        return results

    def _has_variant(
        self, icao: str, *, sim: str | None = None, freeware: bool | None = None
    ) -> bool:
        for variant in self.variants_for(icao):
            if sim and variant.sim not in (sim, "any"):
                continue
            if freeware is not None and variant.freeware != freeware:
                continue
            return True
        return False

    def class_counts(self) -> Counter[str]:
        return Counter(t.aircraft_class for t in self.types.values())

    def validate(self) -> list[str]:
        """Return human-readable problems. An empty list means the catalogue is sane."""
        problems: list[str] = []
        seen: set[str] = set()

        for icao, t in self.types.items():
            if icao != t.icao:
                problems.append(f"{icao}: key does not match icao field ({t.icao})")
            if icao in seen:
                problems.append(f"{icao}: duplicate type")
            seen.add(icao)
            if not (3 <= len(icao) <= 4) or not icao.isalnum():
                problems.append(f"{icao}: implausible type designator")
            if t.aircraft_class not in CLASSES:
                problems.append(f"{icao}: unknown class {t.aircraft_class!r}")
            if t.engine not in ENGINES:
                problems.append(f"{icao}: unknown engine {t.engine!r}")
            if t.wake not in WAKES:
                problems.append(f"{icao}: unknown wake category {t.wake!r}")
            if t.engines < 0 or t.engines > 8:
                problems.append(f"{icao}: implausible engine count {t.engines}")
            if not 100 <= t.cruise_tas_kt <= 1200:
                problems.append(f"{icao}: implausible cruise TAS {t.cruise_tas_kt}")
            if not 50 <= t.range_nm <= 9000:
                problems.append(f"{icao}: implausible range {t.range_nm}")
            if not 20 <= t.cruise_fl <= 600:
                problems.append(f"{icao}: implausible cruise level {t.cruise_fl}")
            if t.ceiling_ft < t.cruise_fl * 100:
                problems.append(f"{icao}: ceiling below cruise level")
            if t.mtow_kg <= 0:
                problems.append(f"{icao}: missing MTOW")
            if t.min_runway_ft < 0 or t.min_runway_ft > 15000:
                problems.append(f"{icao}: implausible runway requirement {t.min_runway_ft}")
            if t.min_runway_ft == 0 and t.aircraft_class != "helicopter":
                problems.append(f"{icao}: zero runway requirement outside helicopters")
            if t.climb_fpm <= 0 or t.descent_fpm <= 0:
                problems.append(f"{icao}: missing climb/descent rate")

        variant_ids: set[str] = set()
        for v in self.variants:
            if v.id in variant_ids:
                problems.append(f"variant {v.id}: duplicate id")
            variant_ids.add(v.id)
            if v.icao not in self.types:
                problems.append(f"variant {v.id}: unknown type {v.icao}")
            if v.sim not in SIMS:
                problems.append(f"variant {v.id}: unknown sim {v.sim!r}")
            if v.fidelity not in FIDELITY:
                problems.append(f"variant {v.id}: unknown fidelity {v.fidelity!r}")

        return problems


def _to_int(value: str, field: str, *, source: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source}: field {field!r} is not numeric: {value!r}") from exc


def _to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def _read_rows(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return [row for row in reader if row.get("icao")]


def load_catalog(data_dir: Path | None = None) -> AircraftCatalog:
    """Load the shipped catalogue, adding a generic variant where none exists."""
    directory = data_dir or DATA_DIR
    type_rows = _read_rows(directory / "aircraft_types.csv")
    variant_rows = _read_rows(directory / "aircraft_variants.csv")

    types: dict[str, AircraftType] = {}
    for row in type_rows:
        icao = (row.get("icao") or "").strip().upper()
        if not icao:
            continue
        types[icao] = AircraftType(
            icao=icao,
            name=(row.get("name") or "").strip(),
            manufacturer=(row.get("manufacturer") or "").strip(),
            aircraft_class=(row.get("class") or "").strip().lower(),
            engine=(row.get("engine") or "").strip().lower(),
            wake=(row.get("wake") or "").strip().upper(),
            engines=_to_int(row.get("engines", "0"), "engines", source=icao),
            range_nm=_to_int(row.get("range_nm", "0"), "range_nm", source=icao),
            cruise_tas_kt=_to_int(row.get("cruise_tas_kt", "0"), "cruise_tas_kt", source=icao),
            cruise_fl=_to_int(row.get("cruise_fl", "0"), "cruise_fl", source=icao),
            ceiling_ft=_to_int(row.get("ceiling_ft", "0"), "ceiling_ft", source=icao),
            mtow_kg=_to_int(row.get("mtow_kg", "0"), "mtow_kg", source=icao),
            min_runway_ft=_to_int(row.get("min_runway_ft", "0"), "min_runway_ft", source=icao),
            climb_fpm=_to_int(row.get("climb_fpm", "0"), "climb_fpm", source=icao),
            descent_fpm=_to_int(row.get("descent_fpm", "0"), "descent_fpm", source=icao),
            taxi_min=_to_int(row.get("taxi_min", "0"), "taxi_min", source=icao),
            confidence=(row.get("confidence") or "").strip().lower(),
            notes=(row.get("notes") or "").strip(),
        )

    variants: list[AircraftVariant] = []
    for row in variant_rows:
        variant_id = (row.get("id") or "").strip()
        icao = (row.get("icao") or "").strip().upper()
        if not variant_id or not icao:
            continue
        variants.append(
            AircraftVariant(
                id=variant_id,
                icao=icao,
                name=(row.get("name") or "").strip(),
                developer=(row.get("developer") or "").strip(),
                sim=(row.get("sim") or "").strip().lower(),
                fidelity=(row.get("fidelity") or "").strip().lower(),
                freeware=_to_bool(row.get("freeware") or ""),
                notes=(row.get("notes") or "").strip(),
            )
        )

    covered = {v.icao for v in variants}
    for icao, aircraft in types.items():
        if icao in covered:
            continue
        variants.append(
            AircraftVariant(
                id=f"generic-{icao.lower()}",
                icao=icao,
                name=f"{aircraft.name} (generic)",
                developer="Generic",
                sim="any",
                fidelity="standard",
                freeware=False,
                notes="No specific add-on listed; performance is for the airframe",
            )
        )

    return AircraftCatalog(types=types, variants=tuple(variants))
