"""Typed view of the live network feed.

The parser is deliberately forgiving -- the feed contains empty strings, numeric
strings, observers in the controller list, and flight plans that were never flown --
but it is not silent: every coercion is centralised here so a shape change upstream
shows up in one place instead of in a model three layers down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import net
from .callsign import Position
from .callsign import parse as parse_callsign
from .config import ENDPOINTS, Settings

__all__ = [
    "Atis",
    "Controller",
    "FeedError",
    "FlightPlan",
    "Pilot",
    "Snapshot",
    "fetch_snapshot",
    "parse_snapshot",
]

_TS_FRACTION = re.compile(r"^(.*\.\d{6})\d*(.*)$")


class FeedError(RuntimeError):
    """The feed was reachable but unusable (bad shape, empty, stale)."""


def parse_timestamp(text: str | None) -> datetime | None:
    """Parse feed timestamps, which carry up to 7 fractional digits."""
    if not text:
        return None
    raw = text.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        match = _TS_FRACTION.match(raw)
        if not match:
            return None
        try:
            parsed = datetime.fromisoformat(match.group(1) + match.group(2))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _hhmm_to_minutes(value: str) -> int | None:
    """``"1008"`` -> 608 minutes. ``HHMM``, not a clock time."""
    if not value or not value.isdigit():
        return None
    number = int(value)
    hours, minutes = divmod(number, 100)
    if minutes >= 60 or hours > 24:
        return None
    return hours * 60 + minutes


@dataclass(frozen=True, slots=True)
class FlightPlan:
    departure: str
    arrival: str
    alternate: str
    aircraft_short: str
    aircraft_full: str
    flight_rules: str
    cruise_altitude_ft: int
    cruise_tas_kt: int
    deptime_hhmm: str
    enroute_minutes: int | None
    fuel_minutes: int | None
    route: str
    remarks: str

    @property
    def has_route(self) -> bool:
        return bool(self.route.strip())

    @property
    def is_complete(self) -> bool:
        return bool(self.departure and self.arrival and self.has_route)


@dataclass(frozen=True, slots=True)
class Pilot:
    cid: int | None
    callsign: str
    latitude: float | None
    longitude: float | None
    altitude_ft: int
    groundspeed_kt: int
    heading: int
    transponder: str
    logon_time: datetime | None
    last_updated: datetime | None
    flight_plan: FlightPlan | None

    @property
    def airborne(self) -> bool:
        return self.altitude_ft > 500 or self.groundspeed_kt > 40

    @property
    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None


@dataclass(frozen=True, slots=True)
class Controller:
    cid: int | None
    callsign: str
    frequency_mhz: float | None
    facility_code: int
    rating: int
    server: str
    visual_range_nm: int
    logon_time: datetime | None
    last_updated: datetime | None
    position: Position


@dataclass(frozen=True, slots=True)
class Atis:
    callsign: str
    airport: str
    atis_code: str
    frequency_mhz: float | None


@dataclass(slots=True)
class Snapshot:
    updated_at: datetime | None
    update_raw: str
    version: int
    connected_clients: int
    unique_users: int
    pilots: list[Pilot] = field(default_factory=list)
    controllers: list[Controller] = field(default_factory=list)
    atis: list[Atis] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)
    prefiled: int = 0

    # ---- derived views -------------------------------------------------

    @property
    def atc(self) -> list[Controller]:
        """Controllers that can actually provide a service (no observers, no ATIS)."""
        return [c for c in self.controllers if c.position.is_atc]

    @property
    def observers(self) -> list[Controller]:
        return [c for c in self.controllers if c.position.is_observer]

    @property
    def pilots_with_plan(self) -> list[Pilot]:
        return [p for p in self.pilots if p.flight_plan is not None]

    @property
    def airborne(self) -> list[Pilot]:
        return [p for p in self.pilots if p.airborne]

    def age_seconds(self, now: datetime | None = None) -> float | None:
        if self.updated_at is None:
            return None
        now = now or datetime.now(UTC)
        return (now - self.updated_at).total_seconds()


def _parse_flight_plan(payload: dict) -> FlightPlan:
    return FlightPlan(
        departure=_text(payload.get("departure")).upper(),
        arrival=_text(payload.get("arrival")).upper(),
        alternate=_text(payload.get("alternate")).upper(),
        aircraft_short=_text(payload.get("aircraft_short")).upper(),
        aircraft_full=_text(payload.get("aircraft")),
        flight_rules=_text(payload.get("flight_rules")).upper(),
        cruise_altitude_ft=_int(payload.get("altitude")) or 0,
        cruise_tas_kt=_int(payload.get("cruise_tas")) or 0,
        deptime_hhmm=_text(payload.get("deptime")),
        enroute_minutes=_hhmm_to_minutes(_text(payload.get("enroute_time"))),
        fuel_minutes=_hhmm_to_minutes(_text(payload.get("fuel_time"))),
        route=_text(payload.get("route")),
        remarks=_text(payload.get("remarks")),
    )


def _parse_pilot(payload: dict) -> Pilot:
    plan_payload = payload.get("flight_plan")
    return Pilot(
        cid=_int(payload.get("cid")),
        callsign=_text(payload.get("callsign")).upper(),
        latitude=_float(payload.get("latitude")),
        longitude=_float(payload.get("longitude")),
        altitude_ft=_int(payload.get("altitude")) or 0,
        groundspeed_kt=_int(payload.get("groundspeed")) or 0,
        heading=_int(payload.get("heading")) or 0,
        transponder=_text(payload.get("transponder")),
        logon_time=parse_timestamp(payload.get("logon_time")),
        last_updated=parse_timestamp(payload.get("last_updated")),
        flight_plan=_parse_flight_plan(plan_payload) if isinstance(plan_payload, dict) else None,
    )


def _parse_controller(payload: dict) -> Controller:
    callsign = _text(payload.get("callsign")).upper()
    facility_code = _int(payload.get("facility")) or 0
    frequency = _float(payload.get("frequency"))
    # 199.998 is the "unset" sentinel, not a frequency.
    if frequency is not None and (frequency <= 0 or abs(frequency - 199.998) < 0.001):
        frequency = None
    return Controller(
        cid=_int(payload.get("cid")),
        callsign=callsign,
        frequency_mhz=frequency,
        facility_code=facility_code,
        rating=_int(payload.get("rating")) or 0,
        server=_text(payload.get("server")),
        visual_range_nm=_int(payload.get("visual_range")) or 0,
        logon_time=parse_timestamp(payload.get("logon_time")),
        last_updated=parse_timestamp(payload.get("last_updated")),
        position=parse_callsign(callsign, facility_code),
    )


def _parse_atis(payload: dict) -> Atis:
    callsign = _text(payload.get("callsign")).upper()
    return Atis(
        callsign=callsign,
        airport=callsign.split("_")[0] if callsign else "",
        atis_code=_text(payload.get("atis_code")),
        frequency_mhz=_float(payload.get("frequency")),
    )


def parse_snapshot(payload: dict) -> Snapshot:
    """Build a :class:`Snapshot` from the raw feed document."""
    if not isinstance(payload, dict):
        raise FeedError("feed payload is not an object")
    general = payload.get("general")
    if not isinstance(general, dict):
        raise FeedError("feed payload has no 'general' section")

    pilots = [_parse_pilot(p) for p in payload.get("pilots") or [] if isinstance(p, dict)]
    controllers = [
        _parse_controller(c) for c in payload.get("controllers") or [] if isinstance(c, dict)
    ]
    atis = [_parse_atis(a) for a in payload.get("atis") or [] if isinstance(a, dict)]
    servers = [
        _text(s.get("ident") or s.get("name") or s.get("hostname"))
        for s in payload.get("servers") or []
        if isinstance(s, dict)
    ]

    return Snapshot(
        updated_at=parse_timestamp(_text(general.get("update_timestamp"))),
        update_raw=_text(general.get("update")),
        version=_int(general.get("version")) or 0,
        connected_clients=_int(general.get("connected_clients")) or 0,
        unique_users=_int(general.get("unique_users")) or 0,
        pilots=pilots,
        controllers=controllers,
        atis=atis,
        servers=[s for s in servers if s],
        prefiled=len(payload.get("prefiles") or []),
    )


def fetch_snapshot(settings: Settings) -> Snapshot:
    """Fetch and parse one snapshot. Raises :class:`FeedError` on unusable data."""
    payload, response = net.get_json(ENDPOINTS["feed"], settings)
    if payload is None:
        raise FeedError(f"feed request failed: {response.error} (status {response.status})")
    snapshot = parse_snapshot(payload)
    if not snapshot.pilots:
        raise FeedError("feed returned zero pilots")
    return snapshot
