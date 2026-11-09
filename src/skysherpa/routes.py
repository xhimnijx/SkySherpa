"""Filed-route tokenisation.

The feed gives us tens of thousands of human-vetted routes a month. To use them as
a corpus we need to split a route string into typed tokens: SIDs/STARs, airways,
fixes, direct segments, and level/speed annotations.

This is a classifier over a genuinely ambiguous grammar -- ``N45`` is an airway,
``N0490F360`` is a level/speed block, ``BPK7F`` is a procedure, ``GATBA`` is a fix,
and only the surrounding context disambiguates the rest. The heuristics below are
deliberate and documented; P1 validates them against the corpus and publishes the
misclassification rate.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["ParsedRoute", "RouteToken", "TokenKind", "classify", "parse_route"]

#: ``N0490F360`` -- speed/level block (TAS 490 kt, FL360).
_LEVEL_RE = re.compile(r"^([NKM])(\d{3,4})([SFAM])(\d{2,4})$")
#: ``FL350``.
_FL_RE = re.compile(r"^FL\d{2,3}$")
#: ``350`` / ``35000`` -- bare level, rare but seen in hand-written routes.
_BARE_LEVEL_RE = re.compile(r"^\d{2,5}$")
#: ``UN857``, ``L612``, ``Q812``, ``N45``, ``A1``.
_AIRWAY_RE = re.compile(r"^[A-Z]{1,2}\d{1,4}[A-Z]?$")
#: ``UVDUS1A``, ``BPK7F``, ``MATIX1``, ``DVR5D``.
_PROCEDURE_RE = re.compile(r"^[A-Z]{3,6}\d[A-Z]?$")
#: ``GATBA``, ``VLS``, ``RAKOM``.
_FIX_RE = re.compile(r"^[A-Z]{2,5}$")

_DIRECT = frozenset({"DCT", "DIRECT"})


class TokenKind(StrEnum):
    AIRWAY = "airway"
    FIX = "fix"
    PROCEDURE = "procedure"
    DIRECT = "direct"
    LEVEL = "level"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RouteToken:
    kind: TokenKind
    text: str

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.text}<{self.kind.value}>"


@dataclass(slots=True)
class ParsedRoute:
    raw: str
    tokens: list[RouteToken] = field(default_factory=list)

    @property
    def path(self) -> list[RouteToken]:
        """Tokens that describe geometry (levels removed)."""
        return [t for t in self.tokens if t.kind is not TokenKind.LEVEL]

    @property
    def normalized(self) -> str:
        """Canonical route identity: uppercase, level annotations stripped."""
        return " ".join(t.text for t in self.path)

    @property
    def route_hash(self) -> str:
        return hashlib.sha1(self.normalized.encode("utf-8")).hexdigest()[:16]

    @property
    def n_fixes(self) -> int:
        return sum(1 for t in self.tokens if t.kind is TokenKind.FIX)

    @property
    def n_airways(self) -> int:
        return sum(1 for t in self.tokens if t.kind is TokenKind.AIRWAY)

    @property
    def n_procedures(self) -> int:
        return sum(1 for t in self.tokens if t.kind is TokenKind.PROCEDURE)

    @property
    def has_direct(self) -> bool:
        return any(t.kind is TokenKind.DIRECT for t in self.tokens)

    @property
    def levels_ft(self) -> list[int]:
        """Cruise levels mentioned in the route, in feet, where parseable."""
        out: list[int] = []
        for token in self.tokens:
            if token.kind is not TokenKind.LEVEL:
                continue
            level = _level_to_ft(token.text)
            if level is not None:
                out.append(level)
        return out

    @property
    def is_empty(self) -> bool:
        return not self.path

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return self.normalized


def _level_to_ft(text: str) -> int | None:
    if _FL_RE.match(text):
        return int(text[2:]) * 100
    if m := _LEVEL_RE.match(text):
        level_type = m.group(3)
        value = int(m.group(4))
        # `F` is a flight level (hundreds of feet); `S` is metric, `A` is altitude in
        # hundreds of feet. Anything else is not a level we can use.
        if level_type in ("F", "A"):
            return value * 100
        return None
    if text.isdigit() and 2 <= len(text) <= 3:
        return int(text) * 100
    if text.isdigit() and len(text) >= 4:
        return int(text)
    return None


def classify(token: str) -> TokenKind:
    """Classify a single (already slash-split) route token."""
    if token in _DIRECT:
        return TokenKind.DIRECT
    if _LEVEL_RE.match(token) or _FL_RE.match(token) or _BARE_LEVEL_RE.match(token):
        return TokenKind.LEVEL
    if _AIRWAY_RE.match(token):
        return TokenKind.AIRWAY
    if _PROCEDURE_RE.match(token):
        return TokenKind.PROCEDURE
    if _FIX_RE.match(token):
        return TokenKind.FIX
    return TokenKind.UNKNOWN


def parse_route(text: str) -> ParsedRoute:
    """Tokenise a filed route string.

    Handles the slash form (``VMV/N0494F340``) by splitting it into its parts, which
    is how level changes are embedded mid-route in real flight plans.
    """
    route = ParsedRoute(raw=text or "")
    cleaned = (text or "").upper().replace(",", " ").replace("..", " ")
    for chunk in cleaned.split():
        for part in chunk.split("/"):
            part = part.strip().strip(".")
            if not part:
                continue
            route.tokens.append(RouteToken(kind=classify(part), text=part))
    return route
