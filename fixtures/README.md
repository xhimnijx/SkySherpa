# Fixtures

Test data lives here. Nothing in this directory may contain a real member identifier.

Intended contents (P1):

| Path | Purpose |
| --- | --- |
| `snapshots/` | Redacted real feed snapshots -- one per representative condition (quiet weekday, busy event evening, feed outage) |
| `vatspy-sample.dat` | A trimmed `VATSpy.dat` for parser tests |
| `boundaries-sample.geojson` | A handful of polygons covering the awkward cases: a parent FIR, a sub-sector, and an orphan TMA |
| `sim/` | Deterministic output of `skysherpa simulate` for CI |

Redaction rules: `cid` -> a stable fake integer, `name` -> `PILOT <n>`, `callsign` ->
a generated callsign from the same facility/suffix grammar (so the parser is still
exercised), `remarks` -> emptied. Everything else is kept verbatim, because the point
of a fixture is to be dirty in the same way the real feed is dirty.

Generate them with `scripts/capture_fixture.py` (P1) rather than by hand.
