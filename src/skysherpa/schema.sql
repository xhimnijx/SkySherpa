-- SkySherpa operational schema (SQLite, WAL).
--
-- This is the P1 contract, checked in before the code that fills it so the modelling
-- work in P2 has a stable target. Design notes live in PLAN.md section 7.
--
-- Conventions
--   * every timestamp is UTC ISO-8601 text (`2026-11-09T21:49:41Z`)
--   * `slot` is a 15-minute bin, 0..95, UTC
--   * durations are seconds unless the column says `_min`
--   * member identifiers appear only as `pseudo_id` (HMAC with a local salt, ADR-0003)

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- ------------------------------------------------------------------ bookkeeping

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- One row per poll attempt. Never deleted: this is the network's heartbeat.
CREATE TABLE IF NOT EXISTS snapshot_stat (
    ts                    TEXT PRIMARY KEY,
    feed_update           TEXT NOT NULL,
    version               INTEGER NOT NULL,
    connected_clients     INTEGER NOT NULL,
    unique_users          INTEGER NOT NULL,
    pilots                INTEGER NOT NULL,
    controllers           INTEGER NOT NULL,
    atc_positions         INTEGER NOT NULL,
    observers             INTEGER NOT NULL,
    atis                  INTEGER NOT NULL,
    prefiled              INTEGER NOT NULL,
    fetch_ms              INTEGER,
    parse_ms              INTEGER,
    accepted              INTEGER NOT NULL DEFAULT 1,
    reject_reason         TEXT
);

-- Windows where we could not observe. Forecasts must not read absence as "offline".
CREATE TABLE IF NOT EXISTS ingest_gap (
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    PRIMARY KEY (started_at, reason)
);

-- --------------------------------------------------------------- static reference

CREATE TABLE IF NOT EXISTS airport (
    icao          TEXT PRIMARY KEY,
    name          TEXT,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    iata          TEXT,
    fir_code      TEXT,
    elevation_ft  INTEGER,
    longest_runway_ft INTEGER,
    source        TEXT NOT NULL DEFAULT 'vatspy',
    refreshed_at  TEXT
);

CREATE TABLE IF NOT EXISTS fir (
    code        TEXT PRIMARY KEY,
    name        TEXT,
    prefix      TEXT,
    callsign    TEXT,
    boundary    TEXT,          -- GeoJSON geometry, only for FIRs we resolve often
    min_lat     REAL,
    min_lon     REAL,
    max_lat     REAL,
    max_lon     REAL
);

-- --------------------------------------------------------------- observed state

-- Every distinct staffing unit we have ever seen.
CREATE TABLE IF NOT EXISTS position (
    position_key  TEXT PRIMARY KEY,   -- e.g. `EGLL_TWR`, `LON_S_CTR`
    facility      TEXT NOT NULL,      -- TWR/APP/CTR/GND/DEL/FSS
    airport_icao  TEXT,
    fir_code      TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

-- One row per continuous online period. `session_key` merges reconnects.
CREATE TABLE IF NOT EXISTS controller_session (
    session_key    TEXT PRIMARY KEY,
    position_key   TEXT NOT NULL REFERENCES position(position_key),
    pseudo_id      TEXT NOT NULL,
    callsign       TEXT NOT NULL,
    facility_code  INTEGER NOT NULL,
    frequency_mhz  REAL,
    rating         INTEGER,
    server         TEXT,
    started_at     TEXT NOT NULL,
    ended_at       TEXT NOT NULL,
    duration_s     INTEGER NOT NULL DEFAULT 0,
    n_samples      INTEGER NOT NULL,
    is_observer    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_session_position_time
    ON controller_session(position_key, started_at);
CREATE INDEX IF NOT EXISTS idx_session_started ON controller_session(started_at);
-- Reconnect merging looks up the most recent session for a callsign.
CREATE INDEX IF NOT EXISTS idx_session_callsign
    ON controller_session(callsign, ended_at);

-- The training matrix for the coverage model. One row per position/day/slot that we
-- actually observed; missing rows are unknown, not offline.
CREATE TABLE IF NOT EXISTS position_slot (
    position_key  TEXT NOT NULL REFERENCES position(position_key),
    day           TEXT NOT NULL,   -- YYYY-MM-DD (UTC)
    slot          INTEGER NOT NULL CHECK (slot BETWEEN 0 AND 95),
    online        INTEGER NOT NULL CHECK (online IN (0, 1)),
    n_samples     INTEGER NOT NULL,
    PRIMARY KEY (position_key, day, slot)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_slot_day ON position_slot(day);

CREATE TABLE IF NOT EXISTS flight (
    flight_id        INTEGER PRIMARY KEY,
    callsign         TEXT NOT NULL,
    pseudo_id        TEXT NOT NULL,
    dep_icao         TEXT,
    arr_icao         TEXT,
    alternate_icao   TEXT,
    aircraft_short   TEXT,
    aircraft_full    TEXT,
    flight_rules     TEXT,
    cruise_alt_ft    INTEGER,
    cruise_tas_kt    INTEGER,
    filed_deptime    TEXT,          -- HHMM as filed
    filed_enroute_min INTEGER,
    filed_route      TEXT,
    route_hash       TEXT,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    off_block        TEXT,
    on_block         TEXT,
    block_min        INTEGER,
    max_gs_kt        INTEGER,
    n_track_points   INTEGER DEFAULT 0,
    parser_version   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_flight_callsign ON flight(callsign, first_seen);
CREATE INDEX IF NOT EXISTS idx_flight_dep_arr ON flight(dep_icao, arr_icao);

-- Simplified trajectory (RDP, 0.5 nm). Not the raw feed; ~25 points per flight.
CREATE TABLE IF NOT EXISTS flight_track (
    flight_id  INTEGER NOT NULL REFERENCES flight(flight_id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    ts         TEXT NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    alt_ft     INTEGER,
    gs_kt      INTEGER,
    hdg        INTEGER,
    PRIMARY KEY (flight_id, seq)
) WITHOUT ROWID;

-- The route corpus: normalised variants, counted. Powers realism, popularity, and
-- route synthesis (docs/dispatch.md section 4).
CREATE TABLE IF NOT EXISTS route_variant (
    route_hash     TEXT PRIMARY KEY,
    dep_icao       TEXT NOT NULL,
    arr_icao       TEXT NOT NULL,
    normalized     TEXT NOT NULL,
    n_observations INTEGER NOT NULL DEFAULT 0,
    first_seen     TEXT,
    last_seen      TEXT,
    med_alt_ft     INTEGER,
    med_block_min  INTEGER,
    distance_nm    REAL
);
CREATE INDEX IF NOT EXISTS idx_variant_pair ON route_variant(dep_icao, arr_icao);

CREATE TABLE IF NOT EXISTS pair_stat (
    dep_icao      TEXT NOT NULL,
    arr_icao      TEXT NOT NULL,
    n_flights     INTEGER NOT NULL DEFAULT 0,
    distance_nm   REAL,
    med_block_min INTEGER,
    last_seen     TEXT,
    PRIMARY KEY (dep_icao, arr_icao)
) WITHOUT ROWID;

-- Demand history: observed departures/arrivals, and the filed-intent nowcast that
-- sits alongside it (PLAN.md section 9.4).
CREATE TABLE IF NOT EXISTS airport_hourly (
    icao        TEXT NOT NULL,
    hour_utc    TEXT NOT NULL,
    departures  INTEGER NOT NULL DEFAULT 0,
    arrivals    INTEGER NOT NULL DEFAULT 0,
    declared_departures INTEGER NOT NULL DEFAULT 0,
    declared_arrivals   INTEGER NOT NULL DEFAULT 0,
    peak_concurrent     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (icao, hour_utc)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS weather_cache (
    station     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('metar', 'taf')),
    fetched_at  TEXT NOT NULL,
    flt_cat     TEXT,
    raw         TEXT,
    payload     TEXT,
    PRIMARY KEY (station, kind)
) WITHOUT ROWID;

-- ------------------------------------------------------------------- forecasts

-- Versioned, append-only. Never overwrite a model in place: the backtest harness
-- needs to compare vintages, and a silent model change is indistinguishable from a
-- network change.
CREATE TABLE IF NOT EXISTS forecast (
    model_version TEXT NOT NULL,
    position_key  TEXT NOT NULL,
    weekday       INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    slot          INTEGER NOT NULL CHECK (slot BETWEEN 0 AND 95),
    p             REAL NOT NULL,
    p_lo          REAL,
    p_hi          REAL,
    n_eff         REAL,
    computed_at   TEXT NOT NULL,
    PRIMARY KEY (model_version, position_key, weekday, slot)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS calibration (
    model_version TEXT NOT NULL,
    group_key     TEXT NOT NULL,
    x_lo          REAL NOT NULL,
    y             REAL NOT NULL,
    n             INTEGER NOT NULL,
    PRIMARY KEY (model_version, group_key, x_lo)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS model_run (
    model_version TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,        -- coverage | demand | calibration
    trained_from  TEXT,
    trained_to    TEXT,
    n_positions   INTEGER,
    brier         REAL,
    brier_skill   REAL,
    notes         TEXT,
    created_at    TEXT NOT NULL
);

-- --------------------------------------------------------------- user / dispatch

CREATE TABLE IF NOT EXISTS user (
    user_id     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    home_icao   TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_fleet (
    user_id      INTEGER NOT NULL REFERENCES user(user_id) ON DELETE CASCADE,
    icao_type    TEXT NOT NULL,
    label        TEXT,
    category     TEXT,             -- jet | turboprop | ga | helicopter
    range_nm     INTEGER,
    cruise_tas_kt INTEGER,
    cruise_alt_ft INTEGER,
    min_runway_ft INTEGER,
    notes        TEXT,
    PRIMARY KEY (user_id, icao_type)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS user_airport (
    user_id        INTEGER NOT NULL REFERENCES user(user_id) ON DELETE CASCADE,
    icao           TEXT NOT NULL,
    installed      INTEGER NOT NULL DEFAULT 1,
    favorite       INTEGER NOT NULL DEFAULT 0,
    scenery_rating INTEGER,        -- 0..5, user's own opinion; never inferred
    notes          TEXT,
    PRIMARY KEY (user_id, icao)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS user_pref (
    user_id       INTEGER PRIMARY KEY REFERENCES user(user_id) ON DELETE CASCADE,
    preset        TEXT,
    weights       TEXT NOT NULL,   -- JSON {feature: weight}
    traffic_target REAL,
    max_block_min INTEGER,
    min_block_min INTEGER,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_run (
    run_id      TEXT PRIMARY KEY,
    user_id     INTEGER REFERENCES user(user_id),
    created_at  TEXT NOT NULL,
    query       TEXT NOT NULL,     -- JSON as submitted
    n_candidates INTEGER,
    elapsed_ms  INTEGER,
    model_version TEXT
);

CREATE TABLE IF NOT EXISTS plan_result (
    run_id       TEXT NOT NULL REFERENCES plan_run(run_id) ON DELETE CASCADE,
    rank         INTEGER NOT NULL,
    dep_icao     TEXT NOT NULL,
    arr_icao     TEXT NOT NULL,
    aircraft     TEXT,
    departure_utc TEXT NOT NULL,
    block_min    INTEGER,
    route        TEXT,
    utility      REAL NOT NULL,
    pareto       INTEGER NOT NULL DEFAULT 0,
    features     TEXT,             -- JSON
    confidence   TEXT,             -- JSON intervals + sample size
    explanation  TEXT,             -- JSON contributions + counterfactuals
    PRIMARY KEY (run_id, rank)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS debrief (
    flight_id   INTEGER REFERENCES flight(flight_id),
    user_id     INTEGER REFERENCES user(user_id),
    created_at  TEXT NOT NULL,
    rating      INTEGER,           -- 1..5, the user's own verdict
    predicted   TEXT,              -- JSON: what we said would happen
    observed    TEXT,              -- JSON: what did
    notes       TEXT,
    PRIMARY KEY (flight_id, user_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS event (
    event_id    TEXT PRIMARY KEY,
    name        TEXT,
    kind        TEXT,
    starts_at   TEXT NOT NULL,
    ends_at     TEXT NOT NULL,
    airports    TEXT,              -- JSON array of ICAO
    routes      TEXT,              -- JSON array of {dep, arr, route}
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_window ON event(starts_at, ends_at);
