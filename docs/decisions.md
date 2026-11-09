# Decision log

Short ADRs. Each records the decision, the alternative that lost, and what would make us revisit it.
Append-only; supersede rather than edit.

---

## ADR-0001 — Python 3.11+, stdlib-first, no service dependencies

**Status:** accepted (2026-11-09)

**Decision.** The application is Python. P0 depends on nothing outside the standard library. P1+
adds `fastapi`, `uvicorn`, `pydantic`, `httpx`, `apscheduler`; the science layer adds `numpy`,
`pandas`, `pyarrow`, `scipy`, `scikit-learn`. Storage is SQLite in WAL mode, plus Parquet exports
read by DuckDB. No Postgres, no Redis, no message broker, no Docker requirement.

**Why.** The target is one laptop that is simultaneously running a flight simulator. Every
additional daemon is another thing that dies at 03:00 and another reason to not leave it running.
The workload — one writer appending intervals, a handful of readers, periodic batch refits — is
almost exactly SQLite's sweet spot. The forecasting models are linear algebra on a few million
floats; there is no GPU-shaped problem here.

**Verified.** `pip install --dry-run --only-binary=:all:` resolves the full science + server stack
as wheels on Python 3.14.3 (2026-11-09). No source builds, no compiler needed on Windows.

**Rejected.** TypeScript/Node end-to-end (worse numerical and data ecosystem); Postgres+TimescaleDB
(operationally heavy for one user); DuckDB-only (weaker concurrent-write story than SQLite for the
operational tables).

**Revisit if.** Multi-user hosting becomes a goal, or the presence matrix outgrows memory.

---

## ADR-0002 — Never store raw snapshots; store sessions and simplified trajectories

**Status:** accepted (2026-11-09)

**Decision.** The poller parses every snapshot and writes only derived records: controller session
intervals, flight records, RDP-simplified tracks (~0.5 nm tolerance, ~25 points per flight), route
tokens, and aggregate counters. Gzipped raw deltas are kept for 6 hours for reprocessing, then
dropped.

**Why.** Raw storage is 3.6 GB/day (1.27 MB × 2,880). Derived storage is ~1,000× smaller, fits a
laptop disk for years, and — more importantly — is what every downstream model actually consumes.
Snapshots are also the privacy-sensitive artefact; not keeping them is the simplest possible data
protection policy.

**Cost.** Reprocessing history requires the derived tables to be right the first time. Mitigation: a
6-hour raw buffer allows fixing a parser bug discovered the same day, and the schema carries
`parser_version` so mixed-vintage data is visible rather than silent.

**Revisit if.** A model emerges that genuinely needs per-poll resolution over months.

---

## ADR-0003 — Pseudonymise member IDs; never model individuals

**Status:** accepted (2026-11-09)

**Decision.** `pseudo_id = HMAC-SHA256(local_salt, cid)[:16]`, salt generated locally and stored in
`data/identity.key`. Analytics tables see only pseudonyms. The retention job purges identifiable
data beyond `SKYSHERPA_RETENTION_DAYS` (default 180). No feature may produce a per-person score,
ranking, or prediction — only per-position, per-airport, per-FIR aggregates.

**Why.** The product needs to know *that* a position is staffed, and how consistently, not *who* is
staffing it. Per-person analytics would be both an ethical problem and an optics disaster for a tool
that observes a volunteer network. Pseudonymisation keeps session-continuity logic (needed to merge
reconnects) without keeping identities in the analytical path.

**Enforced by.** A schema check in CI: no column matching `cid|vatsim_id` outside the
`identity_map` table, which only the ingest path may read/write.

**Revisit if.** Never, for the individual-modelling part. The retention window is tunable.

---

## ADR-0004 — Probabilistic forecasts with explicit calibration, not heuristics

**Status:** accepted (2026-11-09)

**Decision.** Coverage is a probability with an interval, produced by a hierarchical Beta-Binomial
model (shrinkage) and corrected by isotonic calibration. Dispatch uses a scenario bootstrap, not
products of independent probabilities. Every model is gated by backtest metrics before it can
influence a recommendation.

**Why.** A heuristic ("EGLL is usually busy in the evening") produces confident nonsense at the
edges, which is exactly where users need help — a Tuesday at 23:00, a new position, a holiday. The
whole premise of the product is that timing is predictable *and* that we can say how predictable.
Point estimates without intervals make the second claim impossible.

**Rejected.** A gradient-boosted classifier per position (better raw AUC eventually, far worse
behaviour on sparse positions, no interpretability, more dependencies); LLM-based prediction
(unfalsifiable, uncalibrated, expensive, and a category error).

**Revisit if.** After 12 months of data, a boosted model beats the hierarchical model on the gate
metrics *and* still produces usable intervals.

---

## ADR-0005 — Linear, bounded, explainable utility instead of a learned ranker

**Status:** accepted (2026-11-09)

**Decision.** `U = Σ w_i f_i(x)` with `f_i ∈ [0,1]`. Weights come from presets and are adjusted by
pairwise preference learning regularised toward the preset. A Pareto front is always shown alongside
the scalar winner.

**Why.** Users must be able to disagree with the ranking and see why. Bounded terms make weights
comparable ("coverage is worth twice as much as scenery to me"). The linear form is also debuggable:
a surprising result decomposes into features, and a feature bug is visible immediately. A learned
pairwise ranker can come later as a *reranker* on top, but it cannot be the foundation, because then
the explanations become post-hoc fiction.

**Revisit if.** Users reliably prefer a learned ranker's ordering *and* we can explain it with SHAP
values that match the decomposition.

---

## ADR-0006 — Server-rendered UI first, single-page app only if the map demands it

**Status:** proposed (P4)

**Decision.** The P4 interface is server-rendered HTML with a small amount of progressive
enhancement (MapLibre for the route ribbon). No build step, no `node_modules` in the critical path.

**Why.** The core interaction is "show me a board of flights with a map" — a page, not an
application. A build pipeline is a second toolchain to maintain on a machine whose job is to be
running 24/7. If the map interaction genuinely needs client state, that is a contained upgrade.

**Revisit if.** Interactive timeline scrubbing becomes the primary interaction.

---

## ADR-0007 — Do not depend on the official history API

**Status:** accepted (2026-11-09)

**Decision.** Historisation is our own poller. `api.vatsim.net/v2/atc/history` is an opportunistic
backfill behind a flag, with a hard timeout and no retry storm.

**Why.** Verified during reconnaissance: the endpoint returned `504 Gateway Timeout` on every
attempt (`limit=1` included). A cold-start strategy that depends on it would fail at exactly the
moment it matters (day zero). Building the history ourselves is slower but certain, and it is the
only way to control resolution, gaps, and quality gates.

**Revisit if.** The endpoint stabilises and offers deeper history than we have accumulated.

---

## ADR-0008 — Ship a curated aircraft catalogue; do not scan the simulator install

**Status:** accepted (2026-11-10)

**Decision.** The aircraft catalogue is a versioned dataset in the repository:
`src/skysherpa/data/aircraft_types.csv` (159 airframes with dispatch numbers — range,
cruise TAS and level, MTOW, runway requirement, climb/descent rates, taxi allowance) and
`aircraft_variants.csv` (195 add-on products tagged by developer, simulator, fidelity,
and freeware). The user's fleet is a local JSON file that migrates to `user_fleet` in P1.

**Why.** Scanning MSFS `Community` or X-Plane `Custom Scenery` was the obvious
alternative, and it is a trap. Marketplace and streamed installs leave no scannable
artefact; package layout changes between simulator versions; and a scan returns names
without the numbers the dispatcher actually needs. Range and runway decide which flights
are *possible* before any score is computed — a name alone cannot do that. A shipped
table works on every machine, reviews in a diff, and can be corrected when someone spots
a wrong figure.

The variant layer is what users actually choose from. Fenix A320, Asobo A320neo and
FlyByWire A32NX are the same request at different fidelity; keeping them distinct in the
catalogue without duplicating performance data is the whole point of the split.

**Trade-off.** The catalogue goes stale as add-ons ship. Mitigated by a `confidence`
column, a one-row pull request to add an add-on, and block-time estimates that are
property-tested for monotonicity and carry a documented ±10% tolerance.

**Revisit if.** A reliable cross-platform scanner emerges — but it would only ever supply
the variant layer, never the performance layer.

