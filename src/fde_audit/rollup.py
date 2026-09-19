"""Text tier (free, no model): build app_usage_rollup from frame metadata.

Counts only active/passive frames as work time (idle excluded). Seconds are
estimated as work-frame-count * interval; re-running is idempotent because
upsert_rollup keys on (person, device, local_day, app_name).
"""

from __future__ import annotations

from .config import Config
from .db.store import Store

WORK_STATES = ("active", "passive")


def build_rollup(cfg: Config, store: Store) -> int:
    """(Re)compute app_usage_rollup for all recorded days. Returns rows upserted."""
    interval = cfg.capture.interval_seconds
    rows = store.query(
        "SELECT person_id, device_id, local_day, app_name,"
        "       COUNT(*) AS frame_count,"
        "       MIN(ts_utc) AS first_seen,"
        "       MAX(ts_utc) AS last_seen"
        "  FROM frames"
        " WHERE activity_state IN (?, ?) AND app_name IS NOT NULL"
        " GROUP BY person_id, device_id, local_day, app_name",
        WORK_STATES,
    )
    for r in rows:
        store.upsert_rollup(
            person_id=r["person_id"],
            device_id=r["device_id"],
            local_day=r["local_day"],
            app_name=r["app_name"],
            seconds_active=int(r["frame_count"] * interval),
            frame_count=int(r["frame_count"]),
            first_seen=int(r["first_seen"]),
            last_seen=int(r["last_seen"]),
        )
    return len(rows)
