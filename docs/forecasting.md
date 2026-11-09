# Forecasting ATC coverage

The model has one job: produce `P(position p is online at time t)` with intervals that mean what
they say, for every position, including ones we have barely seen.

Nothing here needs a GPU. The whole thing is a few million floats and some closed-form updates,
which is why it can refit every 15 minutes on a laptop while a simulator runs.

---

## 1. Notation

| Symbol | Meaning |
| --- | --- |
| `p` | position key (`EGLL_TWR`, `LON_S_CTR`, …) |
| `d ∈ {0..6}` | day-of-week, UTC |
| `s ∈ {0..95}` | 15-minute slot of the UTC day |
| `o_{p,d,s}` | 1 if position online in that slot, 0 otherwise |
| `a_{p,d,s}` | 1 if we have *observability* for that slot (poller up, feed healthy) |
| `w_d` | recency weight for day `d` |
| `g(p)` | group: (facility type, region) |

Observability is not decoration. If the poller was down for three hours, treating those slots as
"offline" would teach the model that controllers stop working at 03:00. Slots without observability
are excluded from both numerator and denominator, and `ingest_gap` is the audit trail.

---

## 2. Presence matrix

For each `(p, date, slot)` we store the binary state plus a coverage flag. Two derived aggregates:

```
n_{p,s} = Σ_d w_d · a_{p,d,s}                    (effective observations)
k_{p,s} = Σ_d w_d · a_{p,d,s} · o_{p,d,s}        (effective online count)
```

with recency weighting `w_d = 0.5 ^ (age_days / H)`, half-life `H = 56` days. Weighting matters:
controller populations turn over, and a position that was busy last autumn but dormant since
March should not dominate the estimate.

Effective sample size is `n_eff = (Σ w)² / Σ w²`, which is what the intervals use — not the raw day
count. Otherwise a year of stale data would look like certainty.

---

## 3. Smoothing

Controllers work shifts, so adjacent slots are informative about each other. Smooth `k` and `n`
with a circular kernel over slots:

```
k̃_{p,s} = Σ_{s'} K(s − s') · k_{p,s'},    K = normalised triangular, bandwidth b = 2 slots (30 min)
ñ_{p,s} = Σ_{s'} K(s − s') · n_{p,s'}
```

Smoothing counts, not rates, is deliberate: a slot with two observations must not get the same
weight as one with two hundred. Bandwidth `b` is tuned in backtest (candidate grid 1–4); it is a
real bias/variance dial — too wide smears the sharp on/off edges of a shift, too narrow and the
evening peak fragments.

Weekday is *not* smoothed into the slot kernel. Instead it is handled by shrinkage (§4): weekday
patterns are real (Tuesday ≠ Saturday) but noisy, so each weekday's estimate is pulled toward the
position's all-week rate with a strength set by how much data that weekday has.

---

## 4. Hierarchical prior (empirical Bayes)

For each group `g` — facility type crossed with region, e.g. *(TWR, UK)*, *(CTR, Europe)* — fit a
Beta prior over the *position-level* rates in that group:

```
m_g = mean_i (k_i / n_i)
v_g = var_i  (k_i / n_i)

a0_g = m_g · ( m_g(1−m_g)/v_g − 1 )
b0_g = (1−m_g) · ( m_g(1−m_g)/v_g − 1 )
```

clipped to `a0, b0 ∈ [0.5, 50]`, with fallbacks when a group has fewer than 20 positions: widen to
facility type globally, then to the network.

**Why this matters.** Most positions are rare. A position observed on three Tuesdays at 21:00 tells
you almost nothing on its own; the group prior tells you it is probably a Tuesday-evening tower like
every other. Shrinkage is what turns a sparse, gappy dataset into a usable product. The posterior
mean:

```
p̂_{p,s} = (a0_g + k̃_{p,s}) / (a0_g + b0_g + ñ_{p,s})
```

and the 90% interval is the Beta quantile pair. Positions with a lot of data converge to their
empirical rate; positions with little data sit near the group mean with a wide interval. Exactly
the behaviour we want.

---

## 5. Overdispersion

Binomial intervals assume independent days. Reality: a controller takes a week off, then works five
evenings in a row. Days are correlated, so nominal 90% intervals would cover ~70% of outcomes — the
classic way to look confident and be wrong.

Estimate the intra-position correlation `ρ` from the lag-1 autocorrelation of residuals across
consecutive days, then inflate the posterior variance:

```
Var_inflated = Var_beta · (1 + (n_eff − 1)·ρ)
```

with `ρ` clipped to `[0, 0.35]`. This is a one-parameter fix for a structural problem, and it is
verified by the interval-coverage gate (§7). If coverage drifts outside `[0.85, 0.95]`, `ρ` is
re-estimated before anything else is touched.

---

## 6. Calibration

Shrinkage fixes small samples; it does not guarantee calibrated probabilities. So: rolling-origin
backtest, then a monotone correction.

1. For each of the last 8 weeks, train strictly on data before the cutoff, predict the week.
2. Pool predictions by group; bin into deciles; compare predicted mean to observed frequency.
3. Fit **isotonic regression** (PAVA) per group: `p_cal = iso_g(p_raw)`.
4. Store the knot table with the model version; apply at inference.

Isotonic rather than Platt because the distortion is not assumed logistic — a position that is
either always or never online produces a step-shaped reliability curve. The map is monotone, so it
can never invert the ranking of two positions, only rescale them.

Guardrails: a calibration map needs ≥ 300 predictions per group (`calibration_min_samples`) or it
is not fitted; the gate is applied to the *calibrated* output, and the raw-vs-calibrated Brier
scores are both logged so a regression is visible.

---

## 7. Gates

| Metric | Definition | Gate |
| --- | --- | --- |
| Brier skill score | `1 − BS_model / BS_climatology` | ≥ 0.25 |
| Reliability | max &#124;predicted − observed&#124; across deciles | ≤ 0.05 |
| Interval coverage | fraction of outcomes inside the 90% interval | 0.85 – 0.95 |
| AUC | discrimination of online vs. offline | ≥ 0.80 |

Climatology baseline = the position's pooled rate with no time structure. If the model cannot beat
that, it is not adding information and does not ship.

---

## 8. From positions to routes

Given a candidate flight: geometry `G`, departure `t0`, block time `T`.

1. **Sample** `G` every 10 nm, plus one sample either side of every terminal-area boundary.
2. For each sample point `x` and time `t = t0 + (flown distance / speed)`, enumerate the
   **service set** `S(x, t)` — positions that could legally be working that point:
   - TWR: within its configured radius of its airport (terminal phase only);
   - APP: within its TMA, or its FIR when top-down;
   - CTR: point inside the FIR polygon (or sector polygon where available);
   - FSS: the FIR, as the fallback layer.
3. Per-layer probability from the model, then combine across *layers*:

```
P(covered at x, t) = 1 − Π_{l ∈ layers} (1 − p_l(x, t))
```

   Independence across layers is a deliberate approximation and a good one: a CTR providing
   top-down and the underlying TWR are rarely online simultaneously, because the TWR controller
   *is* the person on CTR.

4. **Aggregate** over the timeline (Δt = 1 min):
   - `expected_atc_minutes = Σ_t Δt · P(covered at t)`
   - `P(≥70% covered)` and `P(continuous coverage)` — from §9, not from the product of per-minute
     probabilities.
   - `expected_handoffs` = expected number of changes in the controlling entity along the timeline.
   - `longest_unicom_run` — the distribution of the worst gap, which is what pilots actually feel.

**Ceiling honesty:** if no position exists that could ever serve a segment (deep ocean, unstaffed
region), its probability is zero and no amount of modelling changes that. Those segments are
reported as *structural unicom*, distinct from *scheduling unicom*, because the first is a route
choice and the second is bad luck.

---

## 9. Scenario bootstrap

Per-minute independence is wrong; sessions last hours. The predictive distribution therefore comes
from resampling **whole days**.

```
for b in 1..B:                                  B = 2000
    d_b ← sample historical day, weekday-matched, recency-weighted
    pattern_b ← the actual staffing vector of every position on day d_b
    reweight pattern_b by each position's recent-level correction
    replay the flight timeline against pattern_b
    → (covered_minutes, longest_gap, handoffs, phase coverage)
```

The output is an empirical predictive distribution over exactly the quantities the dispatcher
scores. No distributional assumption, no independence assumption, and the correlation structure —
within a position across time, and across positions within a region — is inherited from reality
rather than invented.

**Recent-level correction.** A position that was online every Tuesday in March but has not appeared
since June should not be resampled as likely. Each sampled day is reweighted by the ratio of the
position's recent rate to its rate in the sampled day, capped to avoid a single day dominating.
This buys adaptivity without abandoning the correlation structure.

**Cost.** 2,000 scenarios × ~180 one-minute samples = 360k lookups per candidate. Vectorised, that
is ~15 ms; Monte Carlo therefore runs only on the final ~50 candidates, never on the full search
space.

---

## 10. Demand nowcast

The feed contains the future: every filed plan declares `deptime` and `enroute_time`.

```
declared_arrivals(airport, window) = Σ over online flights with that arrival whose
                                      deptime + enroute_time falls in the window
```

For horizons ≤ 10 h this beats any historical model, because it is not a prediction — it is a
census of intent. Caveats: pilots cancel, depart late, and fly slower than filed; calibrate a
lateness distribution per phase of flight and apply convolution rather than trusting the filed
time.

Beyond ~10 h, horizon demand decays to zero (almost nobody has filed yet) and the seasonal model
takes over. Blend with inverse-variance weights:

```
w_nowcast = σ²_seasonal / (σ²_seasonal + σ²_nowcast)
```

Reported as a single curve with a widening band, not as two numbers the user has to reconcile.

---

## 11. What cannot be predicted

Worth writing down so the product does not overpromise:

- **A first-time controller** on a position never seen before. The group prior gives a floor and a
  wide interval; that is the honest answer.
- **Exams and one-off events** — unless the events feed is wired in (§9.6 of the plan).
- **Personal emergencies** that end a shift early. This is why continuity risk is reported
  separately from expected coverage.
- **Network outages.** Rare, but they invalidate everything; the gap ledger marks them.
- **A new scenery release or a YouTube video** that sends 200 people to one airport next weekend.

The product's answer to all of these is the same: wide intervals, visible sample sizes, and a
narrow "sure things" filter for users who care about certainty more than optimality.
