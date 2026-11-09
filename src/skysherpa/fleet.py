"""The user's fleet: which aircraft they actually own and want to fly.

Stored as JSON for now (``data/fleet.json``). It migrates to ``user_fleet`` in SQLite
when P1 lands the store; the shape is deliberately the same, so the move is a loader
change and nothing else.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .aircraft import AircraftCatalog

__all__ = ["Fleet", "FleetEntry"]

_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class FleetEntry:
    icao: str
    variant_id: str
    label: str = ""


class Fleet:
    """A tiny persistent set of aircraft. One file, human-editable, no database."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> list[FleetEntry]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        entries = payload.get("entries") or []
        out: list[FleetEntry] = []
        for item in entries:
            icao = str(item.get("icao", "")).strip().upper()
            if not icao:
                continue
            out.append(
                FleetEntry(
                    icao=icao,
                    variant_id=str(item.get("variant_id", "")),
                    label=str(item.get("label", "")),
                )
            )
        return out

    def save(self, entries: list[FleetEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _FORMAT_VERSION,
            "entries": [asdict(entry) for entry in entries],
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def add(
        self,
        icao: str,
        catalog: AircraftCatalog,
        *,
        variant_id: str | None = None,
        label: str = "",
    ) -> FleetEntry:
        aircraft = catalog.get(icao)
        if aircraft is None:
            raise KeyError(f"unknown aircraft type: {icao}")
        if variant_id:
            variant = catalog.variant(variant_id)
            if variant is None:
                raise KeyError(f"unknown variant: {variant_id}")
            if variant.icao != aircraft.icao:
                raise ValueError(f"variant {variant_id} is a {variant.icao}, not a {aircraft.icao}")
        else:
            variants = catalog.variants_for(aircraft.icao)
            variant_id = variants[0].id if variants else f"generic-{aircraft.icao.lower()}"

        entries = [e for e in self.load() if e.icao != aircraft.icao]
        entries.append(
            FleetEntry(
                icao=aircraft.icao,
                variant_id=variant_id,
                label=label or aircraft.name,
            )
        )
        self.save(entries)
        return entries[-1]

    def remove(self, icao: str) -> bool:
        key = icao.strip().upper()
        entries = self.load()
        remaining = [e for e in entries if e.icao != key]
        if len(remaining) == len(entries):
            return False
        self.save(remaining)
        return True

    def clear(self) -> None:
        self.save([])

    def __len__(self) -> int:
        return len(self.load())
