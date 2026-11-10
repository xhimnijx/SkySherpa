# Data sources

Everything here was verified live on **2026-11-09 between 21:48Z and 21:56Z** unless marked
otherwise. Sizes are decompressed unless stated. This file is the source of truth for endpoint
behaviour; when a parser breaks, check here first.

Method: direct HTTP from a Windows host with `curl`/`Invoke-RestMethod`, identifying as a normal
browser user agent, one request at a time.

---

## 1. Core feed — live network state

```
GET https://data.vatsim.net/v3/vatsim-data.json
```

| Property | Value |
| --- | --- |
| Auth | none |
| Server refresh | every 15 s (`general.update` increments) |
| Payload | 1.27 MB JSON; ≈320 KB gzipped |
| Latency observed | 0.7–1.5 s |
| Encoding | gzip supported |

Structure (`version: 3`):

```
general      { version, reload, update: "20261109214941", update_timestamp, connected_clients, unique_users }
pilots[]     1204 in the sampled snapshot
controllers[]  99 in the sampled snapshot (includes observers — see §5)
atis[]         89
servers[]      10
prefiles[]    131
facilities, ratings, pilot_ratings, military_ratings   (enumerations)
```

### 1.1 Pilot record (real sample, name redacted)

```json
{
  "cid": 1819799, "name": "…", "callsign": "MMZ456", "server": "UK",
  "pilot_rating": 0, "military_rating": 0,
  "latitude": 30.29436, "longitude": 3.32372, "altitude": 33256, "groundspeed": 477,
  "transponder": "2231", "heading": 299, "qnh_i_hg": 30.18, "qnh_mb": 1022,
  "flight_plan": {
    "flight_rules": "I", "aircraft": "B77L/H-SDE1E2E3FGHIJ2J3J4J5M1RWXY/LB1D1",
    "aircraft_faa": "H/B77L/L", "aircraft_short": "B77L",
    "departure": "FQMA", "arrival": "LPPT", "alternate": "LPFR",
    "cruise_tas": "496", "altitude": "33000",
    "deptime": "1325", "enroute_time": "1008", "fuel_time": "1213",
    "remarks": "PBN/A1B1… DOF/261109 REG/N772LR EET/… RMK/TCAS SIMBRIEF /V/",
    "route": "UVDUS1A ATUSI UB529 VMV/N0494F340 UB529 GADBA UG652 VLS …"
  },
  "logon_time": "2026-11-09T02:32:11Z", "last_updated": "2026-11-09T21:49:40Z"
}
```

Notes that shape the parser:

- `altitude`, `cruise_tas`, `deptime` are **strings**, not numbers; `deptime` is `HHMM` UTC;
  `enroute_time` is `HHMM` too (`"1008"` = 10 h 08 min) — parse as duration, not as a time.
- `aircraft` is a wake-category/equipment string; `aircraft_short` is the useful type (`B77L`).
- `remarks` carries SimBrief's `DOF/`, `REG/`, `EET/` metadata — useful for quality checks, not
  for logic.
- `route` mixes SIDs (`UVDUS1A`), airways (`UB529`), fixes (`GADBA`), and level/speed
  annotations (`VMV/N0494F340`).

### 1.2 Controller record (real sample)

```json
{
  "cid": 1759694, "name": "Danny", "callsign": "VOZ016A", "frequency": "199.998",
  "facility": 0, "rating": 1, "server": "USA-EAST", "visual_range": 40,
  "text_atis": null, "last_updated": "…", "logon_time": "…"
}
```

`facility` enumeration: `0` observer, `1` FSS, `2` delivery, `3` ground, `4` tower, `5` approach,
`6` enroute. **A record with `facility: 0` is not ATC**, even if the callsign looks like an airline
flight — this is the single most common data-cleaning mistake.

---

## 2. Transceivers (voice topology)

```
GET https://data.vatsim.net/v3/transceivers-data.json
```

481 KB, same 15 s refresh. One entry per callsign:

```json
[{ "callsign": "EGLL_TWR",
   "transceivers": [{ "id": 1234, "frequency": 118500000, "latDeg": 51.47, "lonDeg": -0.46,
                      "heightMslM": 30, "heightAglM": 30 }] }]
```

`frequency` is **Hz** (integer), unlike the controller record's MHz string. This is the raw material
for voice-reachability modelling (PLAN §9.7): it tells you where a controller's radios actually are,
not just where they claim to be.

---

## 3. Official APIs

| Endpoint | Status | Notes |
| --- | --- | --- |
| `GET https://api.vatsim.net/v2/atc/online` | ✅ works | `[{id, callsign, start, server, rating, fp}]` — good for session reconciliation, no position data |
| `GET https://api.vatsim.net/v2/atc/history?limit=N&offset=M` | ⚠️ **unreliable** | Documented, paginated (100/page), rich (start/end/rating/handoffs/tracked aircraft) — but **504 Gateway Timeout** on every attempt during reconnaissance, including `limit=1` |
| `GET https://api.vatsim.net/v2/members/…` | not tested | Member profiles; not needed for v1 |
| Events API (vatsim.dev, "Events API 1.0.0") | ❓ base URL unconfirmed | Documented as `GET /:num`; `/api/events`, `/api/events/10`, `/v2/events/10` all 404. Adapter + fallback required (open item O2) |
| AIP API (airport info) | not tested | Likely VATSIM-specific aerodrome data; OurAirports covers v1 needs |

**Consequence:** the design does *not* depend on any history API. Historisation is done by our own
poller; `/v2/atc/history` is an opportunistic backfill behind a feature flag, never a dependency.

---

## 4. Static reference data

### VATSpy data project

```
https://raw.githubusercontent.com/vatsimnetwork/vatspy-data-project/master/VATSpy.dat        (931 KB)
https://raw.githubusercontent.com/vatsimnetwork/vatspy-data-project/master/Boundaries.geojson (2.33 MB)
```

`VATSpy.dat` is pipe-delimited with bracketed sections — verified 19,900 lines:

```
[Countries]   name|code|subdivision?
[Airports]    ICAO|name|lat|lon|IATA|FIR|?
[FIRs]        code|name|prefix|callsign-stem?
[UIRs]        …
[IDL]         …
```

Real lines:

```
EGLL|London Heathrow|51.4775|-0.461389|LHR|EGTT|0
EGTT|London|LON|EGTT
EGTT-C|London ACC (Central) - London|LON_C|EGTT-C
```

That third line is why FIR resolution cannot be pure ICAO matching: `LON_C` is the *callsign stem*
for a sub-sector of EGTT. The resolver must consult `[FIRs]` before falling back to heuristics.

`Boundaries.geojson` is a FeatureCollection of FIR/UIR polygons — the geometry behind
"which FIR is this aircraft in", and therefore "who could be talking to it".

### OurAirports

Public-domain CSV exports (`davidmegginson.github.io/ourairports-data`) for runways, elevations and
surface. Used for aircraft/airport compatibility (runway length vs. type) and for airports that
VATSpy does not carry. Not yet pinned in code — P1 adds checksum-pinned mirrors.

### Weather

```
GET https://aviationweather.gov/api/data/metar?ids=EGLL&format=json
GET https://aviationweather.gov/api/data/taf?ids=EGLL&format=json
```

Free, no key, generous limits. Verified response fields: `fltCat` (`VFR`/`MVFR`/`IFR`/`LIFR`),
`rawOb`, `wdir`/`wspd`, `visib`, `clouds[]`, and for TAF a `fcsts[]` array with change groups and
probabilities. Batch IDs (`ids=A,B,C`) to avoid per-airport calls.

---

## 5. Quirks log (the things that will bite you)

| Quirk | Evidence | Handling |
| --- | --- | --- |
| Cloudflare burst-throttle | Three rapid requests with a custom UA → `403` (140-byte body). Identical requests seconds later → `200`. Different UAs, same pattern; not UA-specific | Treat 403 as retryable with jittered backoff; never poll faster to "get through" |
| Observers in `controllers` | `BAW15`, `DLH489`, `AIC24`, `BORIS_OBS` all with `facility: 0` | Filter on `facility`, not on callsign shape |
| Sentinel frequency | `199.998` on observer records | Treat `199.998`/`0`/null as "no frequency" |
| ATIS double-counting | 89 `atis[]` entries *and* ATIS positions inside `controllers` | Count ATIS from one source only |
| Irregular callsigns | `EDDF_C_TWR`, `LON_S_CTR`, `CLT_APP`, `CZEG_FSS`, `EBCI_GND`, `_SUP`, `_ADM` | `callsign.py` + a corpus of real observed callsigns as tests |
| String numerics | `"altitude": "33000"`, `"cruise_tas": "496"` | Centralised coercion; never `float()` inline |
| `reload` field | Always `1` in observation | Meaningless; use `update_timestamp` for change detection |
| History API timeouts | `/v2/atc/history` → 504 on every call | Degrade gracefully; feature flag |
| Events API 404s | Multiple base-URL guesses failed | Adapter interface; no event awareness if unavailable |

---

## 6. Politeness policy

- **Floor: 15 s** (the server's own refresh). Default **30 s**. Low-power mode **60 s**.
- One in-flight request at a time; no parallel scraping of the same host.
- Honest `User-Agent` with a contact string; honour `Retry-After`; cap backoff at 5 minutes.
- Never store or redistribute raw snapshots beyond the local 6-hour reprocessing buffer.
- No requests at all when the feature does not need them (e.g., `plan` reads the local DB only).

Estimated transfer: **~900 MB/day at 30 s**, ~450 MB/day at 60 s, ~1.7 GB/day at 15 s. This is the
dominant operating cost of the project and the reason 30 s is the default.

---

## 7. Deliberately not used

- Scrapes of `stats.vatsim.net` member profiles or hours (personal data, no analytical need).
- Discord presence, forum activity, or any social signal tied to an individual.
- Any third-party archive of controller identities.
- Real-world NOTAM/weather services beyond METAR/TAF (out of scope, and a safety-optics risk).

---

## 8. Test fixtures captured from the live feed

`tests/data/feed_sample.json` is one real snapshot, trimmed to 30 pilots, 25 controllers, 6 ATIS
and 6 prefiles, with member names removed and CIDs replaced by sequential integers (ADR-0003).
It exists because hand-written payloads never contain the quirks the real feed does — altitude
as a number rather than a string, sentinel frequencies, empty callsigns, seven-digit timestamps,
pilots sitting on the ground with a plan filed.

`scripts/capture_feed_sample.py` regenerates it. Re-capture deliberately, read the diff, and
confirm `test_the_fixture_carries_no_identities` passes before committing: the scrub is the
point of the file, not a nicety.
