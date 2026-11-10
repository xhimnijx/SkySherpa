"""ATC callsign parsing.

VATSIM callsigns are *almost* structured, which is the dangerous kind of almost:

    EGLL_TWR        airport + facility
    EDDF_C_TWR      airport + sub-position + facility
    LON_S_CTR       FIR prefix + sector + facility
    EGTT_CTR        FIR code + facility
    CZEG_FSS        FIR + flight service
    CLT_APP         airport-ish + approach
    BAW15           an observer using an airline callsign (facility == 0)
    1808096_OBS     an observer using their CID

A parser that assumes ``{ICAO}_{TYPE}`` misclassifies a large minority of positions,
which silently corrupts every downstream coverage number. Hence: parse shape here,
resolve identity against the static reference separately.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "FACILITY_BY_CODE",
    "FacilityType",
    "Position",
    "is_observer_like",
    "parse",
]


class FacilityType(StrEnum):
    OBSERVER = "OBS"
    FSS = "FSS"
    DELIVERY = "DEL"
    GROUND = "GND"
    TOWER = "TWR"
    APPROACH = "APP"
    ENROUTE = "CTR"
    ATIS = "ATIS"
    UNKNOWN = "UNK"


#: `facility` enumeration in the VATSIM data feed.
FACILITY_BY_CODE: dict[int, FacilityType] = {
    0: FacilityType.OBSERVER,
    1: FacilityType.FSS,
    2: FacilityType.DELIVERY,
    3: FacilityType.GROUND,
    4: FacilityType.TOWER,
    5: FacilityType.APPROACH,
    6: FacilityType.ENROUTE,
}

#: Suffix -> facility. `_DEP` is a departure position: approach-tier service.
SUFFIX_TO_FACILITY: dict[str, FacilityType] = {
    "OBS": FacilityType.OBSERVER,
    "SUP": FacilityType.OBSERVER,
    "ADM": FacilityType.OBSERVER,
    "FSS": FacilityType.FSS,
    "DEL": FacilityType.DELIVERY,
    "GND": FacilityType.GROUND,
    "TWR": FacilityType.TOWER,
    "APP": FacilityType.APPROACH,
    "DEP": FacilityType.APPROACH,
    "CTR": FacilityType.ENROUTE,
    "ATIS": FacilityType.ATIS,
}

_OBSERVER_SUFFIXES = frozenset({"OBS", "SUP", "ADM"})
_ICAO_RE = re.compile(r"^[A-Z]{4}$")
_SECTOR_RE = re.compile(r"^[A-Z0-9]{1,2}$")


@dataclass(frozen=True, slots=True)
class Position:
    """A parsed ATC callsign.

    ``key`` is the normalised staffing unit used as the primary key of every
    coverage statistic: the full stem plus facility (``EDDF_C_TWR``, ``LON_S_CTR``).
    """

    raw: str
    stem: str
    facility: FacilityType
    is_observer: bool
    sector: str | None = None
    airport: str | None = None
    fir: str | None = None

    @property
    def key(self) -> str:
        return f"{self.stem}_{self.facility.value}" if self.stem else self.raw

    @property
    def is_atc(self) -> bool:
        """True for anything that can actually provide a control service."""
        return not self.is_observer and self.facility not in (
            FacilityType.OBSERVER,
            FacilityType.ATIS,
            FacilityType.UNKNOWN,
        )

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        bits = [self.key]
        if self.airport:
            bits.append(f"apt={self.airport}")
        if self.fir:
            bits.append(f"fir={self.fir}")
        if self.sector:
            bits.append(f"sec={self.sector}")
        return " ".join(bits)


def is_observer_like(callsign: str, facility_code: int | None = None) -> bool:
    """Cheap predicate for the ingestion hot path."""
    if facility_code == 0:
        return True
    text = callsign.strip().upper()
    _, _, suffix = text.rpartition("_")
    if suffix in _OBSERVER_SUFFIXES:
        return True
    return "_" not in text and facility_code is None


def parse(
    callsign: str,
    facility_code: int | None = None,
    known_airports: Collection[str] | None = None,
) -> Position:
    """Parse *callsign*.

    ``facility_code`` is authoritative when present (it comes straight from the feed).
    Without it, the suffix decides; without either, the callsign is treated as an
    observer, which is the safe default inside ``controllers[]``.

    ``known_airports`` disambiguates the 4-letter stem: ``EGTT_CTR`` looks exactly like
    an airport code but is a FIR. When the reference is not available the stem is
    returned as an airport *candidate*, which callers must resolve before trusting.
    """
    text = (callsign or "").strip().upper()
    if not text:
        return Position(raw="", stem="", facility=FacilityType.UNKNOWN, is_observer=True)

    stem = text
    suffix: str | None = None
    if "_" in text:
        stem, _, suffix = text.rpartition("_")

    stem = stem.strip("_")
    suffix_facility = SUFFIX_TO_FACILITY.get(suffix) if suffix else None

    if facility_code is not None:
        facility = FACILITY_BY_CODE.get(facility_code, FacilityType.UNKNOWN)
        # The feed reports ATIS positions with facility 4 (tower). The suffix is the
        # only thing that distinguishes them, so it wins here.
        if suffix_facility is FacilityType.ATIS:
            facility = FacilityType.ATIS
    else:
        facility = suffix_facility or FacilityType.UNKNOWN

    explicit_observer = facility_code == 0 or (suffix in _OBSERVER_SUFFIXES)
    if facility_code is None and suffix is None:
        explicit_observer = True  # airline-style observer, not a position

    sector: str | None = None
    base = stem
    parts = [part for part in stem.split("_") if part]
    if len(parts) >= 2 and _SECTOR_RE.match(parts[-1]) and not parts[-1].isdigit():
        sector = parts[-1]
        base = "_".join(parts[:-1])
    elif len(parts) >= 2 and parts[-1].isdigit():
        # A trailing number is a position index, not part of the airport: OTHH_1_GND is a
        # second ground position *at* OTHH. Left in, it defeats the four-letter test and the
        # airport is lost, so that controller's coverage never reaches the airport.
        base = "_".join(parts[:-1])

    if not _ICAO_RE.match(base):
        # US towers file three-letter stems (OKC_GND) and the airport is KOKC. Only resolve
        # when the prefixed code is actually known: guessing would invent airports.
        airport = (
            f"K{base}"
            if len(base) == 3 and known_airports is not None and f"K{base}" in known_airports
            else None
        )
    elif known_airports is None:
        airport = base  # candidate: may turn out to be a FIR
    else:
        airport = base if base in known_airports else None

    return Position(
        raw=text,
        stem=stem,
        facility=facility,
        is_observer=explicit_observer,
        sector=sector,
        airport=airport,
    )
