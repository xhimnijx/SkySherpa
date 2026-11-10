"""Configuration: environment -> Settings. No file parsing yet (P1 adds toml merge)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DEFAULT_USER_AGENT", "ENDPOINTS", "Settings"]

DEFAULT_USER_AGENT = "SkySherpa/0.1 (VATSIM dispatcher; local install)"

#: Every upstream endpoint the project uses, in one place.
#: Verified live on 2026-11-09; see docs/data-sources.md for behaviour and quirks.
ENDPOINTS: dict[str, str] = {
    "feed": "https://data.vatsim.net/v3/vatsim-data.json",
    "transceivers": "https://data.vatsim.net/v3/transceivers-data.json",
    "atc_online": "https://api.vatsim.net/v2/atc/online",
    "atc_history": "https://api.vatsim.net/v2/atc/history",
    "vatspy_dat": (
        "https://raw.githubusercontent.com/vatsimnetwork/vatspy-data-project/master/VATSpy.dat"
    ),
    "boundaries": (
        "https://raw.githubusercontent.com/vatsimnetwork/"
        "vatspy-data-project/master/Boundaries.geojson"
    ),
    "metar": "https://aviationweather.gov/api/data/metar",
    "taf": "https://aviationweather.gov/api/data/taf",
}


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings. Immutable; construct via :meth:`load`."""

    root: Path
    data_dir: Path
    cache_dir: Path
    user_agent: str = DEFAULT_USER_AGENT
    poll_seconds: int = 30
    timeout_seconds: float = 45.0
    retries: int = 3

    @classmethod
    def load(
        cls,
        root: Path | None = None,
        data_dir: Path | None = None,
    ) -> Settings:
        root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
        env_home = os.environ.get("SKYSHERPA_HOME")
        data = Path(data_dir) if data_dir is not None else Path(env_home or root / "data")
        return cls(
            root=root,
            data_dir=data,
            cache_dir=data / "cache",
            user_agent=os.environ.get("SKYSHERPA_USER_AGENT", DEFAULT_USER_AGENT),
            poll_seconds=int(os.environ.get("SKYSHERPA_POLL_SECONDS", "30")),
            timeout_seconds=float(os.environ.get("SKYSHERPA_TIMEOUT", "45")),
            retries=int(os.environ.get("SKYSHERPA_RETRIES", "3")),
        )

    def ensure_dirs(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
