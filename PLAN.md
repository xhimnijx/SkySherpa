# SkySherpa — Master Plan

**Status:** P0 complete (scaffold + verified data sources). P1 is next.
**Owner:** Maxim Gil
**Last verified against live data:** 2026-11-09, 21:49Z (Monday, peak-ish European evening)

---

## 1. Thesis

VATSIM is a scheduled social network with an unscheduled interface.

At 21:49Z on a Monday, 1,204 aircraft were airborne and 99 "controllers" were connected — but
only a fraction of those 99 were real ATC positions; the rest were observers sitting on airline
callsigns. Somewhere in that gap between *appearing online* and *actually working a frequency* is
the entire user experience of the network.

Two facts follow:

1. **The route is not the bottleneck.** SimBrief, RouteFinder and every virtual airline dispatch
   system already produce plausible routes, mostly for free. Competing there is a losing game.
2. **Coverage is the bottleneck, and coverage is predictable.** ATC positions are staffed by
   volunteers whose lives are periodic: same controller, same weekday, same 19:00–22:00 local
   slot, for years. That is a learnable signal — closer to forecasting a bus timetable than to
   forecasting weather.

So the product is not a route planner. It is a **dispatcher**: an engine that predicts the
network's future state, then searches the space of feasible flights for the ones that maximise
the experience of flying them.

> We are not selling a route. We are selling the forty minutes where a human says your
> callsign correctly.

**Why this is defensible:** the data is public, but the *joint* product — calibrated per-position
coverage forecasting, intent-based demand nowcasting, a time-flexible optimiser with an explicit
utility function, and a preference model learned from the user's own flights — is not something
any public VATSIM tool does today (§3).

---

## 2. Design principles

| # | Principle | Consequence |
| --- | --- | --- |
| P1 | **Probabilities, not vibes.** Every claim is a number with an interval. | No feature ships without a backtest that could falsify it. |
| P2 | **Time is a first-class dimension.** | The same route at 18:00 and 23:00 are different products. The API is built around `when`, not around `where`. |
| P3 | **Explainability is a feature, not a nicety.** | A recommendation you cannot interrogate is a horoscope. Every result decomposes into weighted features plus counterfactuals. |
| P4 | **Laptop-shaped.** | One process, one SQLite file, no GPU, no cloud, < 5 GB/year of storage. 24/7 on a machine that also runs a simulator. |
| P5 | **Hard constraints vs. soft preferences.** | Fleet, installed airports, and time budget are filters. ATC, traffic, weather, novelty are *scores*. Never silently violate a hard constraint to make a score look good. |
| P6 | **Learn from the user, not about them.** | Personalisation lives in the user's own profile. Controller identities are never modelled, ranked, or surfaced (ADR-0003). |
| P7 | **Be a good citizen of the network.** | Poll floor ≥ 15 s, honest user agent, backoff on 403/429, no redistribution of raw member data. |
| P8 | **Honest cold start.** | On day 1 the forecast is bad. Say so, show the sample count, and improve visibly. |

---

## 3. Prior art and the actual gap

| Tool class | What it does well | What it does not do |
| --- | --- | --- |
| SimBrief / RouteFinder / vRoute | Authoritative route generation, fuel, OFP | No notion of whether anyone will be controlling |
| SimAware, VAT-Spy, vatScope, map.vatsim.net | Live situational awareness | No forecasting, no personalisation, no planning |
| VATSIM stats sites / VATStats | Aggregate history, hours, ranks | Not per-position, not predictive, not dispatch-oriented |
| VATSIM events pages | What is on this week | Not joined to your fleet, time, or route |
| Virtual airline dispatch | Schedules, bookings, PIREPs | Static; not network-aware |
| Discord "where should I fly" | Vibes | Vibes |
| Real-world ATFM research | Staffing prediction, demand-capacity balancing | Never applied to volunteer networks with no schedule authority |

**The gap:** nobody closes the loop between *observed controller behaviour* → *calibrated
coverage forecast* → *constrained multi-objective search over flights* → *user feedback*.

Four ingredients are individually unremarkable and jointly novel:

1. **Per-position schedule learning** with hierarchical shrinkage and explicit calibration.
2. **Scenario bootstrap** — resampling real historical days to preserve the correlations that
   independence assumptions destroy. (Coverage is highly autocorrelated: a controller online at
   19:00 is usually still online at 20:00. Multiplying per-hour probabilities is simply wrong.)
3. **"The feed contains the future."** Every filed flight plan carries `deptime` and
   `enroute_time`. That is a declaration of intent, published 1–10 hours ahead, for free. Demand
   for the next shift can be *nowcast*, not forecast — a distinction almost nobody exploits.
4. **A closed loop.** Fly → debrief → predicted vs. observed → recalibrate both the model and the
   user's taste.

Add the experimental layer (§8.7): **voice reachability**, computed from transceiver geometry, to
separate "logged in" from "will actually hear you".

---

## 4. What the user can ask

The query surface is the product. All of these are the same solver with different constraints
relaxed.

```powershell
# Fix nothing but the time budget: the flagship question
skysherpa plan --block 3h

# Fix the origin, let the destination float
skysherpa plan --from EGLL --max-block 3h --aircraft A20N

# Fix the destination, let the origin float, arrive before a deadline
skysherpa plan --to LOWS --arrive-before "fri 19:00" --scenery

# Pin a window, ask for a specific vibe
skysherpa plan --when "sat 18:00-22:00" --vibe busy --avoid EGLL

# Both ends fixed: when should I go?
skysherpa best-time --from EGLL --to LEPA --next 5d --block 2h20

# Fleet-constrained, anywhere, ranked by a Pareto front
skysherpa plan --config a320 --block 2h --anywhere --pareto

# Intelligence, not a flight
skysherpa coverage --fir EGTT --next 7d
skysherpa demand --airport LFPG --next 12h
skysherpa explain <run-id> --rank 3
```

`--block 3h` means *3 hours of your life*, wheels-up to wheels-down, ±15 min by default. The
solver decides whether that is a 250 nm hop in a Q400 or a 1,400 nm A320 leg.

**Filters (hard):** origin set, destination set, installed airports, aircraft in fleet, block time
window, departure/arrival window, max ground time, range/fuel, runway compatibility, avoid-list.
**Scores (soft):** everything in §10.2.

---

## 5. Data sources (verified live, 2026-11-09)

Full detail, including quirks and exact response shapes, in
[docs/data-sources.md](docs/data-sources.md).

| Source | Endpoint | Cadence | Size | Role | Verified quirk |
| --- | --- | --- | --- | --- | --- |
| Live network | `data.vatsim.net/v3/vatsim-data.json` | 15 s server-side | 1.27 MB JSON (~320 KB gz) | Core: pilots, controllers, ATIS, prefiles | Cloudflare 403s bursty clients; recovers with backoff |
| Transceivers | `data.vatsim.net/v3/transceivers-data.json` | 15 s | 481 KB | Voice reach (P6) | Per-callsign arrays of radio sites |
| Online ATC | `api.vatsim.net/v2/atc/online` | on demand | small | Session reconciliation | Works; minimal fields |
| ATC history | `api.vatsim.net/v2/atc/history` | on demand | paginated 100 | Optional backfill | **504 Gateway Timeout** repeatedly — best-effort only |
| Static reference | VATSpy-data-project `VATSpy.dat` | weekly | 931 KB | Airports, FIRs, UIRs, IDL | Pipe-delimited sections; 19.9k lines |
| FIR polygons | `Boundaries.geojson` | weekly | 2.33 MB | Point-in-FIR, coverage geometry | GeoJSON FeatureCollection |
| Weather | `aviationweather.gov/api/data/{metar,taf}` | 5–30 min | small | Wx suitability, wind | Free, no key; gives `fltCat`, `rawOb`, TAF `fcsts` |
| Runways | OurAirports CSV exports | monthly | ~10 MB | Runway length vs. fleet | Public domain |
| **Aircraft** | `src/skysherpa/data/*.csv` (shipped) | with the repo | 159 types / 195 variants | Fleet, block time, runway fit | Curated, not scanned — ADR-0008 |
| Events | VATSIM Events API | 6 h | small | Event-aware boosting | Base URL not yet confirmed → adapter + fallback (open item O2) |
| Routes (optional) | SimBrief API (user key) | on demand | route | Route generation tier 3 | Free key, user-supplied |
| Logbook (optional) | VATSIM Connect OAuth | on login | — | Personalisation, novelty | OAuth2, user-consented |

**Not used, deliberately:** any third-party scrape of controller identities, Discord presence, or
member profiles. See §15.

### 5.1 What the feed actually contains

A single pilot record carries: position (lat/lon/alt/gs/hdg), `logon_time`, the full
`flight_plan` (dep, arr, alternate, aircraft type string, cruise TAS, cruise altitude, `deptime`,
`enroute_time`, `fuel_time`, raw `route`, `remarks` including SimBrief metadata), and
`last_updated`. That is enough to reconstruct:

- **Intent** — where everyone plans to be, and when (the demand nowcast).
- **Behaviour** — where they actually went (track), how long it took (block time).
- **Route corpus** — tens of thousands of real, human-vetted routes per month.

Controller records carry `callsign`, `facility` (0=OBS, 1=FSS, 2=DEL, 3=GND, 4=TWR, 5=APP,
6=CTR), `frequency`, `rating`, `visual_range`, `logon_time`. That is enough to reconstruct
**sessions** — the unit of analysis for everything downstream.

### 5.2 The data is dirtier than it looks

Observed in a single snapshot, and therefore must be handled explicitly:

- Controllers with `facility: 0` and airline callsigns (`BAW15`, `DLH489`) — **observers, not ATC**.
- `frequency: 199.998` — the "unset" sentinel. Never treat as a real frequency.
- Callsign grammar is irregular: `EGLL_TWR`, `EDDF_C_TWR`, `LON_S_CTR`, `LON_CTR`, `CZEG_FSS`,
  `CLT_APP`, `EBCI_GND`, plus `_OBS`, `_SUP`, `_ADM`, and exam/event positions.
- ATIS entries appear both as `controllers` (facility 4, `_ATIS` suffix) and in `atis[]`.
- Pilots with no flight plan, or a plan filed but never departed (`prefiles`).
- Position fields are strings (`"altitude": "33000"`), sometimes empty.

A parser that assumes `{ICAO}_{TYPE}` will misclassify a large minority of positions — which
silently corrupts the coverage model. Hence `skysherpa/callsign.py` and its test corpus of real
observed callsigns.

---

## 6. Architecture

```
        upstream                              OBSERVER  (always on, 1 writer)
┌──────────────────────┐              ┌────────────────────────────────────────────┐
│ vatsim-data.json     │  30 s poll   │ 1. fetch (gzip, retry, backoff, UA)        │
│ transceivers-data    │─────────────▶│ 2. parse → typed records                   │
│ VATSpy.dat           │  weekly      │ 3. diff vs. previous snapshot              │
│ Boundaries.geojson   │─────────────▶│ 4. sessionize (controllers, ATIS, flights)  │
│ OurAirports CSV      │  monthly     │ 5. pseudonymise CIDs                       │
│ aviationweather.gov  │  10 min      │ 6. simplify + persist trajectories         │
└──────────────────────┘              │ 7. quality gates + gap ledger              │
                                      └───────────────┬────────────────────────────┘
                                                      │  SQLite (WAL) + Parquet exports
                                                      ▼
                                      ┌────────────────────────────────────────────┐
                                      │  ORACLE  (every 15 min + on demand)        │
                                      │  · presence matrix  position × day × slot  │
                                      │  · hierarchical Beta-Binomial posterior    │
                                      │  · isotonic calibration per facility group │
                                      │  · day-block scenario bank                 │
                                      │  · demand nowcast from filed intent        │
                                      └───────────────┬────────────────────────────┘
                                                      │  p(online | position, t), scenarios
                                                      ▼
   user profile ──────────────▶┌───────────────────────────────────────────────────┐
   fleet · airports · weights   │  DISPATCHER                                       │
   logbook · feedback           │  plan(when, from?, to?, block, filters) → ranked   │──▶ CLI
                                │  candidates → score → Monte Carlo → Pareto → why  │──▶ HTTP API
                                └───────────────────────────────────────────────────┘──▶ map UI
                                                      ▲
                                                      │ debrief: predicted vs. observed
                                                      └──────────── feedback loop
```

### 6.1 Process model

One supervisor, three roles. `skysherpa up` starts them in one process (laptop default);
each also runs standalone for debugging.

| Command | Role | Concurrency |
| --- | --- | --- |
| `skysherpa ingest` | Poller, sessionizer, writer | Single writer |
| `skysherpa serve` | Read-only API + static UI | Many readers |
| `skysherpa oracle` | Model refit + calibration | Read, write model tables |
| `skysherpa plan` | CLI query | Read-only; works with no server |

SQLite in WAL mode gives concurrent readers alongside one writer, which is exactly this
workload. No Postgres, no Redis, no Docker (ADR-0001).

### 6.2 Storage

- **SQLite** — the operational store: sessions, flights, tracks, forecasts, users, plans.
- **Parquet** — append-only analytical exports (presence matrix, feature snapshots) for
  backtests; DuckDB `ATTACH`es them read-only when a query needs columnar scans.
- **Raw snapshots** — gzipped JSON deltas retained 6 h for reprocessing, then dropped. The
  derived tables are the source of truth, not the feed (ADR-0002).

Budgets (targets, enforced by a test in P1):

| Metric | Budget | Rationale |
| --- | --- | --- |
| Download | ~900 MB/day at 30 s | 1.27 MB × 2,880; gzip ≈ 4:1 |
| Disk growth | ≤ 15 MB/day, ≤ 5 GB/year | Tracks are simplified at flight end |
| Snapshot parse | ≤ 120 ms | Leave the laptop responsive |
| `plan` latency (200 airports) | ≤ 3 s p95 | Interactive |
| Memory (steady) | ≤ 400 MB | Runs next to a simulator |

At 60 s polling the download halves to ~450 MB/day; that is the documented low-bandwidth mode.

---

## 7. Data model

The DDL lives in [`src/skysherpa/schema.sql`](src/skysherpa/schema.sql) and is the contract for
P1. Highlights and the reasoning behind the non-obvious choices:

| Table | Purpose | Non-obvious choice |
| --- | --- | --- |
| `snapshot_stat` | One row per poll: clients, users, pilots, controllers, ATIS | The cheapest possible network-health time series |
| `ingest_gap` | Windows where we were down or the feed was stale | Forecasts must know what they *didn't* see; gaps are data, not noise |
| `controller_session` | One row per position-online interval | Keyed by `session_key = hash(callsign, started_at)` so reconnects merge |
| `position_slot` | Materialised `(position_key, date, slot, online)` | The training matrix; also the scenario bank |
| `flight` | One row per observed flight | `off_block`/`on_block` derived from first/last movement, not from the flight plan |
| `flight_track` | Simplified trajectory | RDP at 0.5 nm; a 3 h flight ≈ 25 points, ~600 B |
| `route_variant` | Normalised filed routes, counted | The route corpus: popularity, realism, and synthesis source |
| `pair_stat` | `(dep, arr)` aggregates | Powers the popularity prior and the "classic" score |
| `forecast` | Calibrated probability per position × weekday × slot | Versioned; never overwritten in place |
| `airport_hourly` | Departures/arrivals per hour per airport | Demand history for congestion modelling |
| `user*` | Profile, fleet, installed airports, weights | Local only; never leaves the machine |

**Identity.** `pseudo_id = HMAC-SHA256(local_salt, cid)[:16]`. The salt is generated on first run
and stored in `data/identity.key` (git-ignored). Analytics tables never see a raw CID. Session
continuity across reconnects works fine on the pseudonym (ADR-0003).

**Position key.** The normalised unit of staffing analysis: `EGLL_TWR`, `EGLL_APP`, `EGTT_CTR`,
`LON_S_CTR`, `LON_CTR`. Derived from callsign + facility code, resolved against the VATSpy
airport and FIR tables so that `LON_CTR` is understood to be a *top-down* enroute position over
the London FIR even though its stem is not an ICAO code.

---

## 8. Engine 1 — Observer (ingest)

### 8.1 State machine per poll

1. `GET` feed with `If-None-Match`/`If-Modified-Since`; short-circuit if `general.update` is
   unchanged (the server's own 15 s latch).
2. Parse into typed records. Reject the snapshot if any invariant fails (see 8.4).
3. **Diff** against the previous snapshot in memory: controllers/ATIS by callsign, pilots by
   callsign.
4. **Sessionize**: a controller present in poll *n* and absent in *n+1* has `ended_at` between the
   two; refine to the mid-point. Require ≥ 2 consecutive polls to open a session (kills flicker).
   Merge sessions with the same callsign separated by < 90 s (reconnects).
5. **Flights**: first appearance with a flight plan opens a flight; last radar contact with
   `groundspeed < 30` or disappearance closes it. Block time = last movement − first movement.
6. **Tracks**: buffer in memory; simplify and write on flight close. Mid-flight restarts recover
   from the buffer written every 5 min.
7. **Quality gates** and `ingest_gap` bookkeeping.

### 8.2 Why intervals, not snapshots

2880 snapshots/day × 1.27 MB = 3.6 GB/day if stored raw. Storing *intervals* — ~1,600 controller
sessions and ~1,200 flights — plus simplified trajectories is ~1,000× smaller and is what every
downstream model actually wants. This single decision is the difference between "a server on my
laptop" and "a data engineering project".

### 8.3 Failure behaviour

| Condition | Response |
| --- | --- |
| 403 / 429 | Exponential backoff with jitter (cap 5 min), honour `Retry-After`; never hammer |
| 5xx / timeout | Retry ×3, then mark gap, keep serving stale state with an explicit age |
| Malformed JSON | Reject snapshot, keep previous state, raise a quality alarm, do not crash the loop |
| Clock skew | Trust `general.update_timestamp` as the authoritative network clock |
| Restart | Resume from the last `snapshot_stat`; if gap < 5 min, stitch; else record a gap |

The 403 observed during reconnaissance (§ docs/data-sources.md) was a burst-throttle, not a ban:
the same client succeeded seconds later. Treat it as retryable — but never as a reason to poll
faster.

### 8.4 Quality gates

Reject or flag a snapshot when: `pilots` empty, `controllers` empty, `connected_clients` drops
> 60% in one poll, timestamps go backwards, or the payload is < 50% of the 7-day median size.
Every rejection becomes a row, because a silent gap corrupts forecasts.

---

## 9. Engine 2 — Oracle (forecasting)

Mathematical treatment in [docs/forecasting.md](docs/forecasting.md). Summary:

### 9.1 Coverage model

For each position key *p*, weekday *d*, 15-minute slot *s*:

- Build a weighted presence matrix from `position_slot`, recency half-life 56 days.
- Smooth over adjacent slots (controllers work shifts, not isolated quarter-hours).
- Shrink weekday-specific rates toward the pooled rate for that position.
- Shrink position rates toward the prior for its group (facility type × region), fitted by
  method of moments across positions — a hierarchical **Beta-Binomial empirical Bayes** model.
- Inflate intervals for overdispersion: schedules are correlated day to day (a controller takes a
  week off, then works five evenings). Binomial intervals would be dishonestly narrow.
- Recalibrate with **isotonic regression** per group using rolling-origin backtests, and store the
  calibration map with the model version.

Output: `P(position online | p, d, s)` with a 90% interval. Cheap, interpretable, no GPU, and it
degrades gracefully with little data — a position seen twice shrinks almost entirely to its group
mean rather than pretending to be precise.

### 9.2 Route-level coverage

For a candidate flight with a geometry and a departure time:

1. Sample the route every 10 nm (plus every terminal-area boundary crossing).
2. For each sample × time, enumerate the positions that *could legally serve it*: TWR within its
   radius of the airport, APP within its TMA (or FIR when top-down), CTR inside the FIR polygon
   (sector polygons where available), FSS as the fallback layer.
3. `P(covered at point, t) = 1 − Π_layers (1 − P(layer staffed at t))` — a coarse independence
   assumption *across layers*, which is defensible because a CTR top-down and its TWR are rarely
   both online.
4. Aggregate: **expected ATC-minutes**, **P(≥70% covered)**, **expected handoffs**, **longest
   expected unicom run**, and the phase decomposition (ground / departure / enroute / arrival).

### 9.3 Scenario bootstrap (the honest version)

Per-slot independence is wrong: coverage is highly autocorrelated. So the *presented* distribution
comes from resampling **whole historical days** (weekday-matched, recency-weighted), replaying
each day's actual staffing pattern against the candidate's timeline. 2,000 paths, vectorised.

This yields real predictive distributions — not just a mean — and it is the mechanism behind
"you have an 82% chance of continuous coverage, with the gap almost certainly in the 40 minutes
after departure". It also handles the awkward case of a position that is 90% reliable but only
ever online in one 3-hour block.

### 9.4 Demand nowcast — "the feed contains the future"

Every filed plan declares `deptime` + `enroute_time`. Aggregating intent gives a *forward*
arrival/departure curve for the next 1–10 hours at every airport, directly from what pilots have
already committed to. Beyond ~10 hours, fall back to seasonality (weekday × hour) fitted on
history, and blend at the boundary with weights proportional to forecast variance.

This is why SkySherpa can say something genuinely useful about *tonight* on day two of having
data — the forward-looking demand signal does not need a year of history.

### 9.5 Busyness and congestion

- **Network load**: pilots online, same shrinkage machinery as coverage, plus event covariates.
- **Airport load**: departures/arrivals per 30 min from observed + declared demand.
- **Congestion risk**: `P(arrivals in your ±15 min arrival window > historical p90)`. Framed as a
  *proxy* for queueing, never as "you will be held" — we cannot observe holds, only pressure.

### 9.6 Events

Join the events feed (airports, routes, start/end) as a covariate: an event at LFPG makes LFPG
coverage near-certain and arrivals near-certain to be busy. Two-sided: it can boost a
recommendation ("fly into the event") or suppress one ("avoid the 90-minute arrival queue"). This
is where the `--vibe` parameter earns its keep.

### 9.7 Experimental — voice reachability

From `transceivers-data.json`, each online position publishes its radio sites with coordinates,
height AGL/MSL, and frequency. Combining VHF line-of-sight (`d ≈ 1.23·√h_ft` nm) with terrain is
out of scope for a laptop; line-of-sight to a smooth earth plus a generous margin is not. That is
enough to answer a question nobody asks: **"is this controller's radio actually pointed at my
route?"** — catching the case where ATC is online at a facility whose transceivers do not reach
your corridor. Ships as an opt-in flag, validated against anecdote before it influences ranking.

---

## 10. Engine 3 — Dispatcher

Detailed spec in [docs/dispatch.md](docs/dispatch.md).

### 10.1 Search

| Query shape | Candidate space | Method |
| --- | --- | --- |
| Fixed dep + arr | routes × departure times | Enumerate route corpus × 30 min grid, refine ±2 h at 10 min |
| Fixed dep, free arr | installed/all airports within range × block window | Vectorised great-circle prefilter → cheap bound → exact top-K |
| Fixed arr, free dep | same, mirrored | Same |
| Free both ends | airports² | Precomputed `opportunity(airport, time)` bound, beam search (width 200), exact top-50 |

Hard filters are applied before scoring (range, runway, block window, avoid-list), so an
infeasible flight can never outrank a feasible one. Cheap bounds use airport-level coverage and
distance only; Monte Carlo runs exclusively on the final ~50.

Vectorised evaluation targets: 40,000 candidate pairs per second in NumPy — enough that a full
"anywhere, 2 hours" search over 500 installed airports is interactive.

### 10.2 Utility

`U = Σ wᵢ · fᵢ(x)`, with explicit, documented transforms. Default weights come from a preset;
the user's weights are learned (§11).

| Feature | Definition | Default transform |
| --- | --- | --- |
| `atc_expected` | Expected fraction of the flight with ATC | linear, saturating at 0.85 |
| `atc_reliability` | `P(≥70% covered)` from scenario bootstrap | linear |
| `atc_continuity` | `1 − P(longest unicom run > 20 min)` | linear |
| `traffic_fit` | Distance from the user's preferred concurrent-traffic level | tent function |
| `congestion` | Arrival-window congestion percentile | inverted, capped |
| `realism` | Route plausibility × pair popularity | linear |
| `novelty` | Unseen pair/region for this user; network rarity | linear, user-weighted |
| `scenery` | Installed/tagged quality at both ends | linear |
| `weather_fit` | VMC/IMC preference, wind, crosswind vs. fleet limits | linear, hard-fail on limits |
| `time_fit` | Local clock plausibility, night factor, deadline slack | piecewise |
| `effort` | Handoff count, oceanic/remote legs, unfamiliar airspace | inverted |
| `aircraft_fit` | Range margin, cruise-altitude sanity, category match | linear + hard-fail |
| `event_bonus` | Departure/arrival inside an official event | signed, configurable |

Two properties matter: every term is bounded in `[0,1]` (so weights are interpretable), and every
term is *recomputable independently* (so the explanation is truthful rather than post-hoc).

### 10.3 Output: Pareto, not a podium

A single "best" answer hides the real trade-off. Results are presented as a Pareto front over
**ATC quality**, **traffic fit**, and **novelty**, with the scalar winner marked. Twelve options
maximum — a dispatch board, not a search results page.

### 10.4 Explanations

For each result:

- **Contributions**: `wᵢ·fᵢ` per feature, ranked — "this wins on coverage (+0.21) and loses on
  congestion (−0.08)".
- **Counterfactuals**: re-solve with departure shifted ±30/60/90 min, or one airport swapped.
- **Evidence**: the coverage ribbon over time, with the intervals, and the sample size behind it.
- **Honesty**: if the top two are within noise, say "these are statistically tied".

The explanation is generated from the decomposition, not by an LLM. (An LLM layer may later
narrate it; it must never invent it.)

---

## 11. Personalisation and the closed loop

- **Cold start**: presets — *Relaxed sightseeing*, *Airline ops realism*, *Max ATC*, *Contrarian
  (avoid crowds)*, *Short hop*.
- **Explicit**: pairwise preferences ("A or B?") → Bradley–Terry logistic model on feature
  differences, L2-regularised toward the preset. Ten comparisons move the ranking; it cannot run
  away with five.
- **Implicit**: which suggestions were actually flown (observed in the feed under the user's
  callsign, or imported from the logbook via VATSIM Connect).
- **Debrief**: after a flight, compare predicted coverage to observed; log the surprise. Feeds
  both calibration (model-level) and taste (user-level).
- **Novelty memory**: the logbook is what makes "somewhere new" meaningful.

Privacy: the profile is a local file. Nothing is uploaded. No accounts.

---

## 12. Evaluation — the receipt

No model ships without passing its gate. The harness is a first-class deliverable, not an
afterthought.

### 12.1 Forecast backtest (rolling origin)

For each of the last 8 weeks: train strictly on data before the cutoff, predict the week,
compare. Metrics per facility group and overall:

| Metric | Gate |
| --- | --- |
| Brier skill score vs. climatology | ≥ 0.25 |
| Reliability (max deviation across deciles) | ≤ 5 pp |
| 90% interval coverage | 0.85 – 0.95 |
| AUC (position online / not) | ≥ 0.80 |

### 12.2 Dispatch backtest (regret)

Replay historical days. For each simulated query, compute hindsight-optimal choice using *actual*
coverage, then measure the regret of the dispatcher's top-1 and of its top-5 set. Report:

- `hit@1` — how often the top pick is in the hindsight top decile;
- `regret@1` — utility lost vs. oracle;
- `coverage@5` — how often the best option appears somewhere in the shortlist.

Targets in P3: `hit@1 ≥ 0.70` vs. a random-realistic baseline, `regret@1 ≤ 0.10`.

### 12.3 Also tested

- Property tests: scorer bounds, monotonicity (more coverage never lowers `atc_expected`), route
  geometry progress, no hard-constraint violations ever.
- Golden tests on a redacted real snapshot: parser output must be stable.
- Soak test: 48 h ingest with zero unhandled exceptions and a storage budget assertion.

---

## 13. Roadmap

Each phase ends with something usable and a gate that can fail.

### P0 — Scaffold and plan ✅
Repo, ADRs, verified source inventory, `doctor`, `sniff`, parser + geo unit tests, and the
aircraft catalogue (159 types, 195 add-on variants) with a block-time model and a local
fleet manager.
**Gate:** `doctor` exits 0 on a healthy machine; `sniff` renders a live network read-out;
`aircraft`/`fleet` round-trip with no network access.

### P1 — Spine (≈ 1 week)
Polling loop, sessionizer, SQLite schema, gap ledger, static reference loader, FIR resolution,
restart-safe resume, systemd/Task Scheduler unit, retention jobs.
**Gate:** 48 h continuous ingest, zero unhandled exceptions, ≤ 15 MB/day, session count reconciles
with `/v2/atc/online` within 10%.

### P2 — Oracle (≈ 1.5 weeks)
Presence matrix, hierarchical model, calibration, scenario bank, demand nowcast, backtest harness,
`coverage` and `demand` commands.
**Gate:** all §12.1 metrics pass on ≥ 6 weeks of data.

### P3 — Dispatcher (≈ 1.5 weeks)
Candidate generation, utility, search, Monte Carlo, Pareto, explanations, `plan` and `best-time`,
JSON API skeleton.
**Gate:** §12.2 targets; `plan` p95 ≤ 3 s; every result explains itself.

### P4 — Surface (≈ 1 week)
`serve` + map UI: coverage ribbon on the route, staffing heat calendar, plan board, shareable
permalink, live "traffic vs. staffing" view.
**Gate:** page interactive < 1.5 s locally; works in Firefox and Chrome; no JS build step required
for a usable fallback.

### P5 — Personal (≈ 1 week)
Profile, fleet, installed airports, presets, pairwise learning, debrief, VATSIM Connect logbook
import, watchlists and notifications ("a ≥80% ATC 2-hour flight opened up for tonight").
**Gate:** preference model beats default preset on held-out choices by ≥ 15 pp after 10
comparisons.

### P6 — Edge (research)
Voice reachability, event simulation ("what would this event do to my flight"), multi-leg
itineraries, "fly the schedule" mode (a full evening of 3 connected legs), mobile PWA,
community-shared anonymous aggregates.

**Sequencing note:** P2 needs ≥ 6 weeks of wall-clock data. Start the ingest on day one of P1 and
develop against the **synthetic network simulator** (below) so P2/P3 are not blocked by the
calendar.

### 13.1 The calendar problem, and the simulator

The hardest scheduling constraint is physics: a 6-week forecast gate cannot be compressed. So:

- **`skysherpa simulate`** generates a synthetic network with realistic controller schedules
  (weekly rhythms, shift patterns, absences, events, seasonal drift) and pilot demand. It exists
  to develop and CI-test the pipeline before real history accumulates, and to generate adversarial
  cases (a position that is 99% reliable but only in one block; a controller who quits in week 3).
- **Backfill** via `/v2/atc/history` is attempted opportunistically, but it 504'd repeatedly during
  reconnaissance and is treated as best-effort, never as a dependency.
- **Cold-start UX**: until the gates pass, results are shown with explicit confidence labels and
  the sample count. Day 1 is honest, not fake.

---

## 14. Risks

| # | Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- | --- |
| R1 | Feed blocked / terms tightened | Medium | Critical | Conservative cadence, honest UA, backoff, local cache; abstract the client behind one interface so an alternative mirror can be swapped in |
| R2 | Sparse data — early forecasts are noise | High | High | Hierarchical shrinkage, wide honest intervals, explicit sample counts, `fly-now` mode that uses live state only |
| R3 | 6-week data maturation blocks P2 | Certain | Medium | Simulator + cold-start UX + P1/P3 work in parallel |
| R4 | Route geometry needs airways we don't have | High | Medium | Corpus-based synthesis (§10, tier 2); optional user navdata import; never fabricate waypoints |
| R5 | Utility is subjective; users disagree | High | Medium | Explicit weights, presets, learned preferences, Pareto instead of a single answer |
| R6 | Overfitting small samples | Medium | High | Shrinkage, rolling-origin backtests, calibration gates, no deep models |
| R7 | Events API undocumented base URL | Medium | Low | Adapter + fallback; feature degrades to "no event awareness" |
| R8 | Privacy / optics of "tracking controllers" | Medium | High | Pseudonyms, no individual features, no public leaderboards (ADR-0003) |
| R9 | Scope creep into a full flight planner | High | Medium | Non-goals (§16), phased gates, SimBrief delegation |
| R10 | Laptop resource contention with the sim | Medium | Low | Hard budgets, 60 s low-power mode, pause on battery |

---

## 15. Ethics and terms

- **No individual controller analytics.** Positions and schedules are modelled; people are not
  ranked, predicted, or exposed. No "best controller" leaderboard — ever.
- **Pseudonymisation by default** (HMAC with a local salt). Raw CIDs are not written to analytics
  tables; retention job purges anything identifiable beyond the configured window.
- **No redistribution** of raw feeds or derived member-level data. If anything is ever published,
  it is aggregate, k-anonymised, and opt-in.
- **Synthetic-first testing.** Fixtures are redacted; the simulator generates data so CI never
  needs live member data.
- **Terms.** VATSIM user agreement and the data API's acceptable-use expectations apply. The tool
  is read-only: it never connects to an FSD server, never transmits, never impersonates a client.
- **Disclaimer, in the UI and the README:** not for real-world aviation. Nothing here is
  airworthy, and METAR/TAF are consumed for simulation flavour only.

---

## 16. Non-goals

Explicitly **not** building: a flight planner or OFP generator (SimBrief wins, and we integrate);
a VATSIM pilot client; a live map clone; a controller scheduling or booking system; a training or
rating tool; anything that automates flying; anything that needs a cloud account to be useful.

---

## 17. Open decisions

| # | Decision | Options | Leaning |
| --- | --- | --- | --- |
| D1 | Name | SkySherpa / Vector / Handoff / your call | SkySherpa is a placeholder; trivially renamed |
| D2 | Interface order | CLI+API first, UI in P4 *(current plan)* vs. UI first | CLI first: it is the fastest way to validate the model |
| D3 | Fleet source | ~~Manual JSON vs. sim scan~~ **Resolved:** shipped catalogue + local fleet file (ADR-0008); no install scanning | Catalogue is the only option that carries performance data and works with streamed or Marketplace installs |
| D4 | History | Start collecting now vs. chase a third-party archive | Start now; treat archives as optional backfill |
| D5 | Home region default | User-selected vs. inferred from IP/timezone | User-selected (no telemetry) |
| D6 | Licence | Private / MIT / AGPL | Private until P3, then decide (AGPL if it ever goes public) |
| D7 | Web stack | Server-rendered + progressive enhancement vs. React/MapLibre | Server-rendered first (P4), React only if the map demands it |

---

## 18. Immediate next actions (P1, in order)

1. `store.py` + `schema.sql` wired: migrations, WAL, single-writer guard, retention.
2. `ingest.py`: the poll loop with the §8.1 state machine, gap ledger, restart resume.
3. `sessionize.py`: controller/ATIS session intervals from consecutive snapshots; reconnect merge.
4. `flights.py`: flight lifecycle + trajectory simplification + block-time derivation.
5. `staticdata.py` in production mode: weekly refresh, checksum pinning, offline fallback.
6. `fir.py`: point-in-FIR with a bounding-box index (currently brute-force in the P0 slice).
7. `simulate.py`: synthetic network generator + fixtures for CI.
8. `oracle/` package skeleton with the presence-matrix builder and a placeholder model, so the
   backtest harness can be written before the real model lands.
9. `skysherpa up`: supervisor running ingest + oracle + serve with structured logs and `/healthz`.
10. Deploy *this week* on the target laptop so the clock starts on real history.

---

## 19. Glossary

**Position key** — normalised staffing unit (`EGLL_TWR`, `LON_CTR`). **Session** — one continuous
period a position is online. **Slot** — 15-minute UTC bin (96/day). **Presence matrix** —
`position × day × slot` binary staffing history. **Scenario bootstrap** — resampling whole
historical days to build a predictive distribution. **Ribbon** — the per-minute coverage
probability band drawn along a route. **Intent** — a filed flight plan's declared future.
