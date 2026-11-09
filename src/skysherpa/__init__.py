"""SkySherpa - a dispatcher for the VATSIM network.

P0 surface: observe the live network, normalise it, and answer questions about it.
The forecasting and dispatch engines described in PLAN.md land in P2/P3.

CLI output is deliberately ASCII-only: Windows consoles default to a legacy code page
and will render an em dash as a replacement character.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
