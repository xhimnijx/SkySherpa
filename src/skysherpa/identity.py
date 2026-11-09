"""Local identity: pseudonymise member IDs before they reach any analytics table.

The product needs to know *that* a position is staffed and how consistently. It never
needs to know *who* is staffing it. So CIDs are replaced with an HMAC of a local salt
the first time they are written, and the analytics schema has no column that could
reconstruct a person (ADR-0003).

The salt is generated once, on this machine, and never leaves it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

__all__ = ["Identity", "ANON"]

ANON = "anon"


class Identity:
    """HMAC-SHA256 pseudonyms with a per-install salt."""

    def __init__(self, salt: bytes) -> None:
        if not salt:
            raise ValueError("identity salt cannot be empty")
        self._salt = salt

    @classmethod
    def load_or_create(cls, path: Path) -> Identity:
        path = Path(path)
        if path.exists():
            salt = bytes.fromhex(path.read_text(encoding="ascii").strip())
            if salt:
                return cls(salt)
        salt = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(salt.hex(), encoding="ascii")
        try:  # best effort on POSIX; a no-op on Windows
            os.chmod(path, 0o600)
        except OSError:
            pass
        return cls(salt)

    @classmethod
    def from_secret(cls, secret: str) -> Identity:
        """Deterministic identity for tests."""
        return cls(hashlib.sha256(secret.encode()).digest())

    def pseudonym(self, cid: int | None) -> str:
        if cid is None:
            return ANON
        digest = hmac.new(self._salt, str(cid).encode(), hashlib.sha256).hexdigest()
        return digest[:16]
