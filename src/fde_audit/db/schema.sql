-- FDE Audit schema (Phase 1).
-- Portability rules (SQLite now, Postgres later):
--   * text UUID ids, no AUTOINCREMENT
--   * timestamps as BIGINT epoch-millis
--   * booleans as INTEGER 0/1
--   * no SQLite-only features in core tables
-- Every shippable table carries person_id (and device_id where relevant) from
-- day one so the org rollout needs no schema migration.

-- people (shippable; identity)
CREATE TABLE IF NOT EXISTS people (
    person_id     TEXT PRIMARY KEY,          -- UUID
    display_name  TEXT NOT NULL,
    department    TEXT NOT NULL,             -- developer | finance | accounting | ...
    email         TEXT,
    created_at    BIGINT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1
);

-- devices (local)
CREATE TABLE IF NOT EXISTS devices (
    device_id     TEXT PRIMARY KEY,          -- UUID
    person_id     TEXT NOT NULL REFERENCES people(person_id),
    hostname      TEXT NOT NULL,
    os            TEXT NOT NULL,
    tz            TEXT NOT NULL,             -- IANA name, disambiguates local_day
    created_at    BIGINT NOT NULL
);

-- frames (local only) — one metadata row per tick, always, never deduped away.
CREATE TABLE IF NOT EXISTS frames (
    id              TEXT PRIMARY KEY,        -- UUID
    person_id       TEXT NOT NULL REFERENCES people(person_id),
    device_id       TEXT NOT NULL REFERENCES devices(device_id),
    ts_utc          BIGINT NOT NULL,         -- epoch millis
    local_day       TEXT NOT NULL,           -- YYYY-MM-DD in device tz
    tz_offset_min   INTEGER NOT NULL,
    app_name        TEXT,
    exe_path        TEXT,
    window_title    TEXT,
    url_domain      TEXT,                    -- nullable; browser URL capture is v2
    idle_ms         BIGINT NOT NULL DEFAULT 0,
    activity_state  TEXT NOT NULL,           -- active | passive | idle
    is_heartbeat    INTEGER NOT NULL DEFAULT 0,
    screenshot_path TEXT,                    -- nullable (dup/idle/denylist/pruned)
    dup_of          TEXT REFERENCES frames(id),
    frame_hash      TEXT,
    pruned          INTEGER NOT NULL DEFAULT 0,
    screen_w        INTEGER,
    screen_h        INTEGER,
    created_at      BIGINT NOT NULL
);

-- analysis_runs (local) — "what's been analyzed" is a watermark, not a per-frame flag.
CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id        TEXT PRIMARY KEY,          -- UUID
    ts_start      BIGINT,                    -- frame window covered
    ts_end        BIGINT,
    model         TEXT,
    started_at    BIGINT NOT NULL,
    finished_at   BIGINT,
    status        TEXT NOT NULL              -- running | done | failed
);

-- summaries (shippable) — per window-session (span of deduped frames).
CREATE TABLE IF NOT EXISTS summaries (
    id              TEXT PRIMARY KEY,        -- UUID
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(run_id),
    person_id       TEXT NOT NULL REFERENCES people(person_id),
    frame_id_start  TEXT NOT NULL REFERENCES frames(id),
    frame_id_end    TEXT NOT NULL REFERENCES frames(id),
    ts_start        BIGINT NOT NULL,
    ts_end          BIGINT NOT NULL,
    frame_count     INTEGER NOT NULL,
    activity        TEXT,
    category        TEXT,
    app_name        TEXT,
    low_value       INTEGER NOT NULL DEFAULT 0,
    vision_model    TEXT,
    logger_model    TEXT,
    tokens          INTEGER
);

-- app_usage_rollup (shippable; derived) — UNIQUE key makes upsert a real upsert.
CREATE TABLE IF NOT EXISTS app_usage_rollup (
    id             TEXT PRIMARY KEY,         -- UUID
    person_id      TEXT NOT NULL REFERENCES people(person_id),
    device_id      TEXT NOT NULL REFERENCES devices(device_id),
    local_day      TEXT NOT NULL,
    app_name       TEXT NOT NULL,
    seconds_active BIGINT NOT NULL DEFAULT 0,
    frame_count    INTEGER NOT NULL DEFAULT 0,
    first_seen     BIGINT,
    last_seen      BIGINT,
    UNIQUE (person_id, device_id, local_day, app_name)
);

-- observations (shippable; Phase 3 — the ledger)
CREATE TABLE IF NOT EXISTS observations (
    id               TEXT PRIMARY KEY,       -- UUID
    person_id        TEXT NOT NULL REFERENCES people(person_id),
    pattern_key      TEXT NOT NULL,
    description      TEXT,
    category         TEXT,
    occurrences      INTEGER NOT NULL DEFAULT 0,
    first_seen       BIGINT,
    last_seen        BIGINT,
    confidence       REAL,
    status           TEXT NOT NULL DEFAULT 'open',  -- open | confirmed | actioned | dismissed
    suggested_upgrade TEXT
);

-- digests (shippable; Phase 4/org)
CREATE TABLE IF NOT EXISTS digests (
    id           TEXT PRIMARY KEY,           -- UUID
    person_id    TEXT NOT NULL REFERENCES people(person_id),
    period       TEXT,
    generated_at BIGINT,
    content_md   TEXT,
    token_cost   INTEGER
);

-- sync_state (org; later) — tracks what's been shipped upward.
CREATE TABLE IF NOT EXISTS sync_state (
    table_name   TEXT NOT NULL,
    last_synced  BIGINT,
    cursor       TEXT,
    PRIMARY KEY (table_name)
);

-- Indexes (from day one).
CREATE INDEX IF NOT EXISTS idx_frames_ts             ON frames(ts_utc);
CREATE INDEX IF NOT EXISTS idx_frames_day_app        ON frames(local_day, app_name);
CREATE INDEX IF NOT EXISTS idx_frames_person_ts      ON frames(person_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_summaries_person_ts   ON summaries(person_id, ts_start);
