"""Thin, driver-agnostic data-access layer.

SQLite backend now; a psycopg backend can be added later selected by config,
keeping the same call sites. All SQL uses '?' placeholders and portable types
(text UUID ids, BIGINT epoch-millis, INTEGER 0/1 booleans).
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def new_id() -> str:
    return str(uuid.uuid4())


class Store:
    """SQLite-backed store. Open with `Store(db_path)`, use as a context manager
    or call `.close()`. `init_db()` applies schema.sql + numbered migrations."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")

    # -- lifecycle --------------------------------------------------------
    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def init_db(self) -> None:
        """Apply the base schema, then any migrations not yet recorded."""
        self.conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at BIGINT NOT NULL)"
        )
        applied = {
            r["version"]
            for r in self.conn.execute("SELECT version FROM schema_migrations")
        }
        if MIGRATIONS_DIR.is_dir():
            from ..clock import now_ms  # local import to avoid cycle at module load

            for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = mig.stem
                if version in applied:
                    continue
                self.conn.executescript(mig.read_text(encoding="utf-8"))
                self.conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, now_ms()),
                )
        self.conn.commit()

    # -- generic helpers --------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def commit(self) -> None:
        self.conn.commit()

    # -- people / devices -------------------------------------------------
    def add_person(
        self,
        display_name: str,
        department: str,
        created_at: int,
        email: Optional[str] = None,
        person_id: Optional[str] = None,
        ext_key: Optional[str] = None,
    ) -> str:
        pid = person_id or new_id()
        self.conn.execute(
            "INSERT INTO people(person_id, display_name, department, email, created_at, active, ext_key)"
            " VALUES (?, ?, ?, ?, ?, 1, ?)",
            (pid, display_name, department, email, created_at, ext_key),
        )
        self.conn.commit()
        return pid

    def get_person(self, person_id: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM people WHERE person_id = ?", (person_id,))

    def get_person_by_ext_key(self, ext_key: str) -> Optional[sqlite3.Row]:
        """Look up a person by their stable natural key (SID / DOMAIN\\user).
        The join point for fleet auto-enrollment — same OS user maps to one row
        across reinstalls and re-pushes."""
        return self.query_one("SELECT * FROM people WHERE ext_key = ?", (ext_key,))

    def set_person_department(self, person_id: str, department: str) -> None:
        """Self-correction path: update a person's department when Active
        Directory later reports a different one."""
        self.conn.execute(
            "UPDATE people SET department = ? WHERE person_id = ?",
            (department, person_id),
        )
        self.conn.commit()

    def list_people(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM people ORDER BY created_at")

    def first_person(self) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM people ORDER BY created_at LIMIT 1")

    def upsert_device(
        self,
        person_id: str,
        hostname: str,
        os_name: str,
        tz: str,
        created_at: int,
        device_id: Optional[str] = None,
    ) -> str:
        """One logical device per (person, hostname). Reuse the id if it exists."""
        existing = self.query_one(
            "SELECT device_id FROM devices WHERE person_id = ? AND hostname = ?",
            (person_id, hostname),
        )
        if existing:
            return existing["device_id"]
        did = device_id or new_id()
        self.conn.execute(
            "INSERT INTO devices(device_id, person_id, hostname, os, tz, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (did, person_id, hostname, os_name, tz, created_at),
        )
        self.conn.commit()
        return did

    def list_devices(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT d.*, p.display_name, p.department FROM devices d"
            " JOIN people p ON p.person_id = d.person_id"
            " ORDER BY d.hostname"
        )

    def device_liveness(self, since_24h: int) -> list[sqlite3.Row]:
        """One row per registered device with the facts liveness is derived from:
        newest frame, its app/state, and recent frame counts. `since_24h` bounds
        the 'frames in the last day' tally. Deriving up/down from `last_frame_ts`
        is the caller's job (it needs the live-window threshold)."""
        return self.query(
            "SELECT d.device_id, d.hostname, d.os, d.person_id,"
            "       p.display_name, p.department,"
            "       f.last_frame_ts, f.frames_total, f.frames_24h,"
            "       lf.app_name AS last_app, lf.activity_state AS last_state,"
            "       lf.is_heartbeat AS last_is_heartbeat"
            " FROM devices d"
            " JOIN people p ON p.person_id = d.person_id"
            " LEFT JOIN ("
            "   SELECT device_id, MAX(ts_utc) AS last_frame_ts,"
            "          COUNT(*) AS frames_total,"
            "          SUM(CASE WHEN ts_utc >= ? THEN 1 ELSE 0 END) AS frames_24h"
            "   FROM frames GROUP BY device_id"
            " ) f ON f.device_id = d.device_id"
            " LEFT JOIN frames lf ON lf.device_id = d.device_id"
            "   AND lf.ts_utc = f.last_frame_ts"
            " GROUP BY d.device_id"
            " ORDER BY f.last_frame_ts DESC, d.hostname",
            (since_24h,),
        )

    # -- capture status (liveness transitions) ----------------------------
    def get_device_status(self, device_id: str) -> Optional[sqlite3.Row]:
        return self.query_one(
            "SELECT * FROM device_status WHERE device_id = ?", (device_id,))

    def upsert_device_status(
        self, device_id: str, person_id: str, status: str,
        last_frame_ts: Optional[int], since_ts: int, updated_at: int,
    ) -> None:
        """Write the current derived state. `since_ts` is only applied on INSERT or
        a real status change; an unchanged status keeps its original `since_ts`
        (so 'up for 3h' keeps counting) while still refreshing last_frame_ts."""
        self.conn.execute(
            "INSERT INTO device_status"
            " (device_id, person_id, status, last_frame_ts, since_ts, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(device_id) DO UPDATE SET"
            "   person_id = excluded.person_id,"
            "   last_frame_ts = excluded.last_frame_ts,"
            "   updated_at = excluded.updated_at,"
            "   since_ts = CASE WHEN device_status.status = excluded.status"
            "                   THEN device_status.since_ts ELSE excluded.since_ts END,"
            "   status = excluded.status",
            (device_id, person_id, status, last_frame_ts, since_ts, updated_at),
        )
        self.conn.commit()

    def insert_status_event(
        self, device_id: str, person_id: str, status: str,
        prev_status: Optional[str], ts: int, last_frame_ts: Optional[int],
        gap_ms: Optional[int],
    ) -> str:
        eid = new_id()
        self.conn.execute(
            "INSERT INTO capture_status_events"
            " (id, device_id, person_id, status, prev_status, ts,"
            "  last_frame_ts, gap_ms, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, device_id, person_id, status, prev_status, ts,
             last_frame_ts, gap_ms, ts),
        )
        self.conn.commit()
        return eid

    def list_status_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query(
            "SELECT e.*, d.hostname, p.display_name"
            " FROM capture_status_events e"
            " LEFT JOIN devices d ON d.device_id = e.device_id"
            " LEFT JOIN people p ON p.person_id = e.person_id"
            " ORDER BY e.ts DESC LIMIT ?",
            (limit,),
        )

    # -- frames -----------------------------------------------------------
    def insert_frame(self, frame: dict[str, Any]) -> str:
        cols = [
            "id", "person_id", "device_id", "ts_utc", "local_day", "tz_offset_min",
            "app_name", "exe_path", "window_title", "url_domain", "idle_ms",
            "activity_state", "is_heartbeat", "screenshot_path", "dup_of",
            "frame_hash", "pruned", "screen_w", "screen_h", "created_at",
        ]
        fid = frame.get("id") or new_id()
        frame = {**frame, "id": fid}
        placeholders = ", ".join("?" for _ in cols)
        self.conn.execute(
            f"INSERT INTO frames({', '.join(cols)}) VALUES ({placeholders})",
            tuple(frame.get(c) for c in cols),
        )
        self.conn.commit()
        return fid

    def last_frame(self, device_id: str) -> Optional[sqlite3.Row]:
        return self.query_one(
            "SELECT * FROM frames WHERE device_id = ? ORDER BY ts_utc DESC LIMIT 1",
            (device_id,),
        )

    # -- rollup -----------------------------------------------------------
    def upsert_rollup(
        self,
        person_id: str,
        device_id: str,
        local_day: str,
        app_name: str,
        seconds_active: int,
        frame_count: int,
        first_seen: int,
        last_seen: int,
    ) -> None:
        """Real upsert on the UNIQUE (person, device, day, app) key."""
        self.conn.execute(
            "INSERT INTO app_usage_rollup"
            " (id, person_id, device_id, local_day, app_name,"
            "  seconds_active, frame_count, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(person_id, device_id, local_day, app_name) DO UPDATE SET"
            "   seconds_active = excluded.seconds_active,"
            "   frame_count    = excluded.frame_count,"
            "   first_seen     = MIN(app_usage_rollup.first_seen, excluded.first_seen),"
            "   last_seen      = MAX(app_usage_rollup.last_seen, excluded.last_seen)",
            (new_id(), person_id, device_id, local_day, app_name,
             seconds_active, frame_count, first_seen, last_seen),
        )
        self.conn.commit()

    # -- analysis: frame reads -------------------------------------------
    def work_frames(
        self,
        person_id: str,
        ts_start: Optional[int] = None,
        ts_end: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        """Active/passive frames for a person in a window, ordered by time.
        Idle frames are excluded — sessions never span them."""
        sql = [
            "SELECT * FROM frames WHERE person_id = ?",
            "  AND activity_state IN ('active', 'passive')",
        ]
        params: list[Any] = [person_id]
        if ts_start is not None:
            sql.append("  AND ts_utc >= ?")
            params.append(ts_start)
        if ts_end is not None:
            sql.append("  AND ts_utc <= ?")
            params.append(ts_end)
        sql.append(" ORDER BY ts_utc ASC")
        return self.query("\n".join(sql), params)

    def analysis_watermark(self, person_id: str) -> Optional[int]:
        """Max ts_end across this person's completed runs — the boundary past
        which frames are unanalyzed. None if nothing has been analyzed."""
        row = self.query_one(
            "SELECT MAX(a.ts_end) AS wm FROM analysis_runs a"
            " WHERE a.status = 'done'"
            "   AND EXISTS (SELECT 1 FROM summaries s"
            "               WHERE s.analysis_run_id = a.run_id AND s.person_id = ?)",
            (person_id,),
        )
        return row["wm"] if row and row["wm"] is not None else None

    # -- analysis: runs ---------------------------------------------------
    def start_analysis_run(
        self, started_at: int, ts_start: Optional[int], ts_end: Optional[int],
        model: Optional[str] = None, run_id: Optional[str] = None,
    ) -> str:
        rid = run_id or new_id()
        self.conn.execute(
            "INSERT INTO analysis_runs(run_id, ts_start, ts_end, model,"
            " started_at, finished_at, status)"
            " VALUES (?, ?, ?, ?, ?, NULL, 'running')",
            (rid, ts_start, ts_end, model, started_at),
        )
        self.conn.commit()
        return rid

    def get_analysis_run(self, run_id: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM analysis_runs WHERE run_id = ?", (run_id,))

    def finish_analysis_run(self, run_id: str, finished_at: int, status: str) -> None:
        self.conn.execute(
            "UPDATE analysis_runs SET finished_at = ?, status = ? WHERE run_id = ?",
            (finished_at, status, run_id),
        )
        # note: no commit here — commit_analysis() wraps run + summaries in one txn

    def fail_analysis_run(self, run_id: str, finished_at: int) -> None:
        self.conn.execute(
            "UPDATE analysis_runs SET finished_at = ?, status = 'failed' WHERE run_id = ?",
            (finished_at, run_id),
        )
        self.conn.commit()

    # -- analysis: summaries ---------------------------------------------
    def insert_summary(self, summary: dict[str, Any]) -> str:
        cols = [
            "id", "analysis_run_id", "person_id", "frame_id_start", "frame_id_end",
            "ts_start", "ts_end", "frame_count", "activity", "category", "app_name",
            "low_value", "vision_model", "logger_model", "tokens",
            # Phase 2 columns (migration 001)
            "capability", "task", "input_density", "friction", "boundary_reason",
        ]
        sid = summary.get("id") or new_id()
        summary = {**summary, "id": sid}
        placeholders = ", ".join("?" for _ in cols)
        self.conn.execute(
            f"INSERT INTO summaries({', '.join(cols)}) VALUES ({placeholders})",
            tuple(summary.get(c) for c in cols),
        )
        return sid

    # -- analysis: observations ledger -----------------------------------
    def capability_app_spread(self, person_id: str) -> list[sqlite3.Row]:
        """Per (capability, app) totals across all this person's summaries — the
        basis for consolidation ('same function across N apps'). Reads the current
        connection so it sees rows inserted earlier in an open transaction."""
        return self.query(
            "SELECT capability, app_name, COUNT(*) AS sessions,"
            "       SUM(frame_count) AS frames, MIN(ts_start) AS first_seen,"
            "       MAX(ts_end) AS last_seen FROM summaries"
            " WHERE person_id = ? AND capability IS NOT NULL AND app_name IS NOT NULL"
            " GROUP BY capability, app_name",
            (person_id,),
        )

    def upsert_observation(
        self, *, person_id: str, pattern_key: str, description: str,
        category: Optional[str], opportunity_type: Optional[str],
        est_minutes_per_week: Optional[float], suggested_upgrade: Optional[str],
        first_seen: int, last_seen: int, occurrences: int,
        confirm_threshold: int, affected_people: int = 1,
        accumulate: bool = True,
    ) -> str:
        """Evidence for a pattern keyed by (person, pattern_key). With
        accumulate=True (default) occurrences/evidence_count add up across runs
        (more sightings = more evidence). With accumulate=False the count is a
        recomputed snapshot (e.g. consolidation's app count) and REPLACES the
        prior value. Status flips to 'confirmed' once evidence crosses the
        threshold (>=3-4 repeats)."""
        existing = self.query_one(
            "SELECT * FROM observations WHERE person_id = ? AND pattern_key = ?",
            (person_id, pattern_key),
        )
        if existing is None:
            oid = new_id()
            status = "confirmed" if occurrences >= confirm_threshold else "open"
            self.conn.execute(
                "INSERT INTO observations(id, person_id, pattern_key, description,"
                " category, occurrences, first_seen, last_seen, confidence, status,"
                " suggested_upgrade, opportunity_type, est_minutes_per_week,"
                " affected_people, evidence_count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (oid, person_id, pattern_key, description, category, occurrences,
                 first_seen, last_seen, None, status, suggested_upgrade,
                 opportunity_type, est_minutes_per_week, affected_people, occurrences),
            )
            return oid
        oid = existing["id"]
        new_occ = (existing["occurrences"] or 0) + occurrences if accumulate else occurrences
        # don't downgrade a human-set status (actioned/dismissed)
        status = existing["status"]
        if status in ("open", "confirmed"):
            status = "confirmed" if new_occ >= confirm_threshold else "open"
        self.conn.execute(
            "UPDATE observations SET occurrences = ?, evidence_count = ?,"
            " last_seen = ?, first_seen = MIN(first_seen, ?), status = ?,"
            " est_minutes_per_week = ?, opportunity_type = COALESCE(?, opportunity_type),"
            " description = ?, suggested_upgrade = COALESCE(?, suggested_upgrade)"
            " WHERE id = ?",
            (new_occ, new_occ, last_seen, first_seen, status,
             est_minutes_per_week, opportunity_type, description,
             suggested_upgrade, oid),
        )
        return oid

    # -- observations: reporting / confirmation (Phase 3) ----------------
    def list_observations(
        self, *, person_id: Optional[str] = None, department: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
        opportunity_types: Optional[Iterable[str]] = None,
        min_minutes: Optional[float] = None,
        order: str = "minutes", limit: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        """The ledger, sliced. Joins `people` so rows carry display_name +
        department (org slicing). order='minutes' ranks by weekly time cost then
        evidence; order='recent' by last_seen. Any filter left None is ignored."""
        sql = [
            "SELECT o.*, p.display_name, p.department",
            " FROM observations o JOIN people p ON p.person_id = o.person_id",
            " WHERE 1=1",
        ]
        params: list[Any] = []
        if person_id is not None:
            sql.append(" AND o.person_id = ?")
            params.append(person_id)
        if department is not None:
            sql.append(" AND p.department = ?")
            params.append(department)
        statuses = list(statuses) if statuses is not None else None
        if statuses:
            sql.append(f" AND o.status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        opps = list(opportunity_types) if opportunity_types is not None else None
        if opps:
            sql.append(f" AND o.opportunity_type IN ({', '.join('?' for _ in opps)})")
            params.extend(opps)
        if min_minutes is not None:
            sql.append(" AND COALESCE(o.est_minutes_per_week, 0) >= ?")
            params.append(min_minutes)
        if order == "recent":
            sql.append(" ORDER BY o.last_seen DESC")
        else:
            sql.append(
                " ORDER BY COALESCE(o.est_minutes_per_week, 0) DESC,"
                " o.evidence_count DESC, o.last_seen DESC")
        if limit is not None:
            sql.append(" LIMIT ?")
            params.append(limit)
        return self.query("\n".join(sql), params)

    def find_observation(
        self, ref: str, person_id: Optional[str] = None
    ) -> list[sqlite3.Row]:
        """Resolve a human-supplied reference to observation rows. Matches (in
        order) exact id, exact pattern_key, then id-prefix — so `digest observe`
        accepts either the short id shown in a report or the pattern_key. Returns
        all matches; the caller errors on 0 or >1."""
        def _q(where: str, extra: tuple) -> list[sqlite3.Row]:
            sql = f"SELECT * FROM observations WHERE {where}"
            args = extra
            if person_id is not None:
                sql += " AND person_id = ?"
                args = extra + (person_id,)
            return self.query(sql, args)

        for where, extra in (
            ("id = ?", (ref,)),
            ("pattern_key = ?", (ref,)),
            ("id LIKE ?", (ref + "%",)),
        ):
            rows = _q(where, extra)
            if rows:
                return rows
        return []

    def set_observation_status(self, obs_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE observations SET status = ? WHERE id = ?", (status, obs_id))
        self.conn.commit()

    def capability_app_spread_all(
        self, department: Optional[str] = None
    ) -> list[sqlite3.Row]:
        """Cross-person generalization of capability_app_spread: per
        (capability, app, person) totals across the whole org (or one
        department). The basis for org-level consolidation — a capability handled
        in N apps by M people is a license-cut candidate."""
        sql = [
            "SELECT s.capability, s.app_name, s.person_id, p.department,",
            "       COUNT(*) AS sessions, SUM(s.frame_count) AS frames,",
            "       MIN(s.ts_start) AS first_seen, MAX(s.ts_end) AS last_seen",
            " FROM summaries s JOIN people p ON p.person_id = s.person_id",
            " WHERE s.capability IS NOT NULL AND s.app_name IS NOT NULL",
        ]
        params: list[Any] = []
        if department is not None:
            sql.append(" AND p.department = ?")
            params.append(department)
        sql.append(" GROUP BY s.capability, s.app_name, s.person_id")
        return self.query("\n".join(sql), params)

    # -- digests (Phase 4) ------------------------------------------------
    def insert_digest(
        self, *, person_id: str, period: Optional[str], generated_at: int,
        content_md: str, token_cost: int = 0, digest_id: Optional[str] = None,
    ) -> str:
        """Persist a generated digest. Person-scoped (the shippable table carries
        person_id from day one)."""
        did = digest_id or new_id()
        self.conn.execute(
            "INSERT INTO digests(id, person_id, period, generated_at, content_md, token_cost)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (did, person_id, period, generated_at, content_md, token_cost),
        )
        self.conn.commit()
        return did

    def list_digests(
        self, person_id: Optional[str] = None, limit: Optional[int] = None,
        department: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        sql = [
            "SELECT d.id, d.person_id, d.period, d.generated_at, d.token_cost,",
            "       p.display_name, p.department",
            " FROM digests d JOIN people p ON p.person_id = d.person_id",
            " WHERE 1=1",
        ]
        params: list[Any] = []
        if person_id is not None:
            sql.append(" AND d.person_id = ?")
            params.append(person_id)
        if department is not None:
            sql.append(" AND p.department = ?")
            params.append(department)
        sql.append(" ORDER BY d.generated_at DESC")
        if limit is not None:
            sql.append(" LIMIT ?")
            params.append(limit)
        return self.query("\n".join(sql), params)

    def find_digest(
        self, ref: str, person_id: Optional[str] = None
    ) -> list[sqlite3.Row]:
        """Resolve a digest reference: exact id, then id-prefix. Returns all
        matches; the caller errors on 0 or >1."""
        for where, extra in (("id = ?", (ref,)), ("id LIKE ?", (ref + "%",))):
            sql = f"SELECT * FROM digests WHERE {where}"
            args = extra
            if person_id is not None:
                sql += " AND person_id = ?"
                args = extra + (person_id,)
            rows = self.query(sql, args)
            if rows:
                return rows
        return []

    # -- retention --------------------------------------------------------
    def frames_to_prune(self, cutoff_ts: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT id, screenshot_path FROM frames"
            " WHERE screenshot_path IS NOT NULL AND pruned = 0 AND ts_utc < ?",
            (cutoff_ts,),
        )

    def mark_pruned(self, frame_id: str) -> None:
        self.conn.execute(
            "UPDATE frames SET pruned = 1, screenshot_path = NULL WHERE id = ?",
            (frame_id,),
        )
