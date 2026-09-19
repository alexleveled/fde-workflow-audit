-- Real-time capture liveness (the "Live capture" panel + `fde_audit status`).
--
-- Liveness itself is DERIVED, not stored: a device is "up" if its most recent
-- frame is within monitor.live_window_seconds. These two tables only record the
-- part that can't be re-derived after the fact — the up/down *transitions*, so
-- an admin can see exactly when a machine stopped (or started) logging even long
-- after the gap. Both carry person_id + device_id so they ship to Postgres at the
-- org rollout with no further migration.

-- device_status: the current derived state, one row per device. This is the
-- memory the sweep compares against to notice a transition; `since_ts` lets the
-- UI say "up for 3h" / "down for 52m".
CREATE TABLE IF NOT EXISTS device_status (
    device_id     TEXT PRIMARY KEY REFERENCES devices(device_id),
    person_id     TEXT NOT NULL REFERENCES people(person_id),
    status        TEXT NOT NULL,             -- up | down (derived at last sweep)
    last_frame_ts BIGINT,                    -- newest frame seen at last sweep
    since_ts      BIGINT NOT NULL,           -- when the current status began
    updated_at    BIGINT NOT NULL            -- last sweep that touched this row
);

-- capture_status_events: append-only transition log (the real-time status log).
-- One row each time a device flips up<->down (and one seeding the first observed
-- state). Never updated — history is the point.
CREATE TABLE IF NOT EXISTS capture_status_events (
    id            TEXT PRIMARY KEY,          -- UUID
    device_id     TEXT NOT NULL REFERENCES devices(device_id),
    person_id     TEXT NOT NULL REFERENCES people(person_id),
    status        TEXT NOT NULL,             -- the NEW status: up | down
    prev_status   TEXT,                      -- prior status (NULL on first observation)
    ts            BIGINT NOT NULL,           -- when the change was detected
    last_frame_ts BIGINT,                    -- newest frame at detection time
    gap_ms        BIGINT,                    -- silence length when going down (NULL for up)
    created_at    BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_status_events_ts ON capture_status_events(ts);
CREATE INDEX IF NOT EXISTS idx_status_events_device ON capture_status_events(device_id, ts);
