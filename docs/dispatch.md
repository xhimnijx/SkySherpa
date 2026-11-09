# The dispatcher

Input: a query. Output: a ranked, explained, Pareto-optimal set of flights. This document is the
specification; the P3 implementation should be readable against it line by line.

---

## 1. Query model

```jsonc
{
  "when": { "from": "2026-11-10T18:00:00Z", "to": "2026-11-10T23:00:00Z" },   // or "anytime"
  "from": null,                    // ICAO | [ICAO] | null  (null = anywhere)
  "to":   null,
  "block_minutes": { "target": 180, "tolerance": 15 },
  "aircraft": ["A20N", "B738"],    // must be in the fleet; null = any owned type
  "airports": { "only_installed": true, "avoid": ["EGLL"], "require": ["LFPG"] },
  "arrive_before": "2026-11-13T18:59:00Z",
  "vibe": "balanced",              // preset name, or explicit weights
  "weights": null,                 // { "atc_expected": 0.4, ... } overrides preset
  "max_results": 25,
  "pareto": true,
  "explain": "full"                // off | brief | full
}
```

**Hard constraints** (violators are removed before scoring): airport membership, aircraft presence,
range and runway, block window, absolute time window, avoid-list, ground-time budget.
**Soft objectives**: everything in §3.

The same query object is accepted by the CLI (flags → query), the HTTP API, and the UI. One code
path, three skins.

---

## 2. Candidate generation

### 2.1 Case A — both ends fixed

Routes from the corpus for `(dep, arr)`, ranked by observation count; if none exist, tier 2
synthesis (§4). Departure times on a 30-minute grid across the window, plus the *edges* of
controller shifts detected in the forecast (a controller coming online at 19:00 is a step change
worth sampling exactly). Cost: `|routes| × |times|` ≈ 10 × 20 = 200 evaluations.

### 2.2 Case B — one end fixed

Destinations = airports within reach:

```
reachable = { apt : great_circle(dep, apt) ≤ range(aircraft) · 0.92 }
            ∩ block_window(tas_cruise, wind_correction)
            ∩ installed_airports
```

Roughly 50k airports worldwide, ~2k after range and block filtering, ~200 after "installed".
Score with a **cheap bound** (airport-level terminal coverage at arrival time + distance fit), keep
the top 400 pairs, then run the exact evaluator. Cost: one vectorised great-circle pass plus 400
exact evaluations.

### 2.3 Case C — neither end fixed

All pairs of installed airports: `|A|²`. For 500 airports that is 250k pairs — feasible only with a
bound that is much cheaper than the evaluator. Precompute an **opportunity index**:

```
O[apt, hour] = expected terminal coverage + traffic-fit term + installed-scenery term
```

then the pair bound is `O[dep, t] + O[arr, t + block] + distance_penalty`. Sort, beam-search with
width 200 (breadth-first over departure times, keeping the best partial options), exact-evaluate the
top 50. The bound is admissible for the coverage and scenery terms; it ignores enroute quality, so
the beam must be wide enough to admit route-diverse candidates — hence 200, tuned in backtest.

### 2.4 Departure-time refinement

Utility is smooth-ish in time but has **cliffs**: a controller's shift start at 19:00 creates a step.
So: coarse grid (30 min) → detect local maxima and cliffs → golden-section refine within ±2 h per
surviving candidate at 10-minute resolution. This finds "leave at 19:10" rather than "leave at
19:00 or 19:30", which is precisely the kind of answer the user cannot get anywhere else.

---

## 3. Utility

```
U(x) = Σ_i w_i · f_i(x)          w_i ≥ 0, Σ w_i = 1 (normalised), f_i ∈ [0,1]
```

| Feature | Formula / source | Transform `f` | Notes |
| --- | --- | --- | --- |
| `atc_expected` | `E[covered minutes] / block` (§ forecasting 8) | `min(1, p/0.85)` | Saturates: 100% vs 90% is not worth much extra |
| `atc_reliability` | `P(covered ≥ 70%)` | linear | The "will I be abandoned" term |
| `atc_continuity` | `1 − P(longest unicom > 20 min)` | linear | Weighted higher for IFR users |
| `traffic_fit` | `&#124;ln(1+concurrent) − ln(1+target)&#124;` | tent: `1 − d/d_max` | `target` from user profile; `busy` preset raises it |
| `congestion` | arrival-window percentile vs. history | `1 − p`, capped at 0.8 | Compressed: doesn't matter until it does |
| `realism` | `0.6·route_plausibility + 0.4·pair_popularity` | linear | `route_plausibility` = share of observed variants ≤ 15% longer |
| `novelty` | unseen pair (1) / unseen region (0.6) / seen (0) | linear | User-weighted; off by default for "airline ops" |
| `scenery` | user-tagged quality at both ends | linear | Only counts *installed* airports |
| `weather_fit` | VMC/IMC preference, wind, crosswind vs. limits | linear, hard-fail on limits | TAF for arrival window, METAR for departure |
| `time_fit` | local departure/arrival clock, night factor, deadline slack | piecewise | 03:00 departures are legal, not enjoyable |
| `effort` | handoffs, oceanic legs, unfamiliar region | `1 − normalised` | Penalises "work" |
| `aircraft_fit` | range margin, cruise-altitude sanity, category match | linear + hard-fail | A 2-hour 747 is not a realistic 747 flight |
| `event_bonus` | dep/arr inside an official event | signed, `[−0.5, +0.5]` | User-configurable direction |

Two invariants, both property-tested:

1. If `atc_expected` rises and nothing else changes, `U` cannot fall.
2. If any hard constraint is violated, `U = −∞` (not merely low).

---

## 4. Route generation (three tiers)

1. **Observed.** Exact normalised variants filed for `(dep, arr)`, with counts, altitude
   distribution, and typical block time. Best realism; empty for unusual pairs.
2. **Synthesised.** A directed graph over route tokens built from the corpus: nodes are fixes and
   airways, edges are observed transitions with frequencies. Candidate routes are k-shortest paths
   weighted by `−log(popularity)`, then spliced (prefix of one observed route + suffix of another
   through a shared fix) to produce plausible-but-novel routes. Validation: monotone progress toward
   the destination, detour ≤ 25% over great circle, no repeated fixes, cruise altitude consistent
   with distance.
3. **External.** SimBrief (user's key) or a pasted route. We score and re-time it; we do not claim
   to have generated it.

Never fabricate a waypoint that appears in no airway and no observed route. A route that does not
exist is worse than no route.

---

## 5. Output contract

```jsonc
{
  "run_id": "01JC…", "generated_at": "…", "query": { … }, "elapsed_ms": 2140,
  "results": [
    {
      "rank": 1,
      "dep": "EGLL", "arr": "LEPA", "aircraft": "A20N",
      "departure_utc": "2026-11-10T19:05:00Z", "block_minutes": 143,
      "route": "BPK7F BPK Q295 BRA DCT …",
      "utility": 0.81,
      "pareto": [1, 0, 1],
      "confidence": { "atc_minutes": [118, 143], "p_ge_70pct": 0.82, "sample_days": 41 },
      "features": { "atc_expected": 0.91, "traffic_fit": 0.74, … },
      "contributions": [["atc_expected", 0.31], ["realism", 0.12], ["congestion", -0.06]],
      "timeline": [
        { "phase": "ground",   "from": "19:05", "to": "19:18", "p_covered": 0.97, "positions": ["EGLL_GND"] },
        { "phase": "departure","from": "19:18", "to": "19:32", "p_covered": 0.94, "positions": ["EGLL_TWR", "LON_CTR"] },
        …
      ],
      "counterfactuals": [
        { "change": "depart +90m", "utility": 0.86, "delta": { "atc_minutes": +34, "congestion": -0.12 } }
      ],
      "caveats": ["coverage after 21:30 rests on 6 sample days"]
    }
  ],
  "pareto_front": [1, 3, 7, 9],
  "notes": ["2 of 25 results are statistically tied"]
}
```

The `caveats` field is not decoration. It is the difference between a tool that is trusted and a tool
that is impressive once.

---

## 6. Explanations

Generated from the decomposition, deterministically:

1. **Why this one** — top three positive and top two negative contributions, in plain language.
2. **What would change it** — counterfactuals from re-solving under perturbations.
3. **How sure** — the interval, the sample size, and the caveat list.
4. **Ties** — results within `1 SE` of each other are reported as tied rather than arbitrarily
   ordered.

An LLM may later *narrate* this JSON. It must never compute it: the numbers the user acts on have to
be traceable to the model, or the whole "calibrated, not confident" premise collapses.

---

## 7. Performance

| Stage | Budget (200 installed airports) |
| --- | --- |
| Constraint filtering + great-circle | 80 ms |
| Cheap bound / beam | 150 ms |
| Exact evaluation, top 400 | 700 ms |
| Monte Carlo, top 50 × 2,000 paths | 900 ms |
| Explanation + serialisation | 150 ms |
| **Total p95** | **≤ 3 s** |

Rules: hard filters first, always; Monte Carlo only on finalists; cache airport-level terms per
`(airport, hour)` for the duration of a run; never re-parse routes inside the hot loop.

---

## 8. How the dispatcher is judged

Replay historical days. For each simulated query, compute the hindsight-optimal result using actual
coverage, then:

| Metric | Definition | Gate |
| --- | --- | --- |
| `hit@1` | share of queries whose top pick lands in the hindsight top decile | ≥ 0.70 |
| `regret@1` | `1 − U(top1)/U(oracle)`, averaged | ≤ 0.10 |
| `coverage@5` | share of queries where the oracle is in the top 5 | ≥ 0.90 |
| `calibration` | predicted vs. realised utility across deciles | within 0.05 |

A dispatcher that is merely *plausible* will pass none of these. That is the point of having them.
