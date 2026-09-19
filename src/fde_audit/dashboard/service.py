"""Read-side data access for the dashboard.

Every method opens its own short-lived `Store` (a fresh sqlite3 connection) so the
service is safe to call from FastAPI's threadpool and from background job threads
— sqlite connections are not shareable across threads. All queries are scoped to
an optional person and an optional [ts_start, ts_end] epoch-ms window.

This layer is read-only. Triggers (rollup / prepare / digest) live in `jobs.py`.
"""

from __future__ import annotations

from typing import Any, Optional

from .. import clock, monitor
from ..config import Config
from ..db.store import Store
from ..reporting import DEFAULT_STATUSES, build_report

WORK_STATES = ("active", "passive")

# named period -> lookback in days ("all" -> None, no lower bound)
PERIODS: dict[str, Optional[int]] = {
    "1d": 1, "7d": 7, "30d": 30, "90d": 90, "all": None,
}


def resolve_window(
    period: Optional[str], ts_from: Optional[int], ts_to: Optional[int]
) -> tuple[Optional[int], Optional[int], str]:
    """Turn a UI period/explicit range into (ts_start, ts_end, label). Explicit
    from/to win; otherwise a named period is a lookback from now."""
    if ts_from is not None or ts_to is not None:
        return ts_from, ts_to, "custom"
    period = period or "7d"
    days = PERIODS.get(period, 7)
    if days is None:
        return None, None, "all"
    now = clock.now_ms()
    return now - days * 86_400_000, now, period


class DashboardService:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # -- helpers ----------------------------------------------------------
    def _store(self) -> Store:
        return Store(self.cfg.db_path)

    @staticmethod
    def _window(
        col: str, person_id: Optional[str],
        ts_start: Optional[int], ts_end: Optional[int],
        alias: str = "", department: Optional[str] = None,
    ) -> tuple[str, list[Any]]:
        """Build a `WHERE`-tail and params for a person and/or department + time
        window. `department` is applied as a subquery over `people`, so it works on
        any table that carries `person_id` (frames, summaries, observations)."""
        pfx = f"{alias}." if alias else ""
        clauses: list[str] = []
        params: list[Any] = []
        if person_id is not None:
            clauses.append(f"{pfx}person_id = ?")
            params.append(person_id)
        if department is not None:
            clauses.append(
                f"{pfx}person_id IN (SELECT person_id FROM people WHERE department = ?)")
            params.append(department)
        if ts_start is not None:
            clauses.append(f"{pfx}{col} >= ?")
            params.append(ts_start)
        if ts_end is not None:
            clauses.append(f"{pfx}{col} <= ?")
            params.append(ts_end)
        where = (" AND " + " AND ".join(clauses)) if clauses else ""
        return where, params

    # -- identity ---------------------------------------------------------
    def people(self) -> list[dict[str, Any]]:
        with self._store() as s:
            return [
                {"person_id": p["person_id"], "display_name": p["display_name"],
                 "department": p["department"], "active": p["active"]}
                for p in s.list_people()
            ]

    def departments(self) -> list[str]:
        with self._store() as s:
            rows = s.query(
                "SELECT DISTINCT department FROM people"
                " WHERE department IS NOT NULL ORDER BY department")
        return [r["department"] for r in rows]

    def default_person_id(self) -> Optional[str]:
        with self._store() as s:
            p = s.first_person()
            return p["person_id"] if p else None

    # -- live capture (real-time liveness) --------------------------------
    def live_capture(self) -> dict[str, Any]:
        """Read-only 'who's capturing right now' snapshot across all devices."""
        with self._store() as s:
            return monitor.evaluate(s, self.cfg)

    def status_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """The status-change log (device up/down transitions), newest first."""
        with self._store() as s:
            return monitor.status_events(s, limit=limit)

    # -- capture / analysis overview -------------------------------------
    def overview(
        self, person_id: Optional[str], ts_start: Optional[int], ts_end: Optional[int],
        department: Optional[str] = None,
    ) -> dict[str, Any]:
        interval = self.cfg.capture.interval_seconds
        fw, fp = self._window("ts_utc", person_id, ts_start, ts_end, department=department)
        with self._store() as s:
            row = s.query_one(
                "SELECT"
                "  COUNT(*) AS frames,"
                "  SUM(CASE WHEN screenshot_path IS NOT NULL AND dup_of IS NULL"
                "           THEN 1 ELSE 0 END) AS images_stored,"
                "  SUM(CASE WHEN dup_of IS NOT NULL THEN 1 ELSE 0 END) AS dup_linked,"
                "  SUM(CASE WHEN activity_state='active' THEN 1 ELSE 0 END) AS active_f,"
                "  SUM(CASE WHEN activity_state='passive' THEN 1 ELSE 0 END) AS passive_f,"
                "  SUM(CASE WHEN activity_state='idle' THEN 1 ELSE 0 END) AS idle_f,"
                "  COUNT(DISTINCT local_day) AS days,"
                "  COUNT(DISTINCT app_name) AS apps,"
                "  MIN(ts_utc) AS first_ts, MAX(ts_utc) AS last_ts"
                f" FROM frames WHERE 1=1{fw}", fp,
            )
            sw, sp = self._window("ts_start", person_id, ts_start, ts_end, department=department)
            srow = s.query_one(
                "SELECT COUNT(*) AS sessions, COALESCE(SUM(frame_count),0) AS frames,"
                "       COALESCE(SUM(CASE WHEN low_value=1 THEN 1 ELSE 0 END),0) AS low_value"
                f" FROM summaries WHERE 1=1{sw}", sp,
            )
            # observations are the current ledger (not window-scoped — they are
            # cumulative evidence); scope only by person / department.
            ow, op = self._window("last_seen", person_id, None, None, department=department)
            obs = s.query_one(
                "SELECT COUNT(*) AS total,"
                "  SUM(CASE WHEN status IN ('open','confirmed') THEN 1 ELSE 0 END) AS open_confirmed,"
                "  COALESCE(SUM(est_minutes_per_week),0) AS minutes"
                f" FROM observations WHERE 1=1{ow}", op,
            )
            # frames awaiting vision analysis — work frames past each person's
            # analysis watermark (mirrors what `analyze prepare` would pick up).
            # Deliberately NOT window-scoped: pending work is pending regardless
            # of the period being viewed.
            uw, up = self._window("ts_utc", person_id, None, None, alias="f",
                                  department=department)
            unrow = s.query_one(
                "SELECT COUNT(*) AS n FROM frames f"
                " WHERE f.activity_state IN ('active','passive')"
                "   AND f.ts_utc > COALESCE("
                "     (SELECT MAX(a.ts_end) FROM analysis_runs a"
                "       WHERE a.status = 'done'"
                "         AND EXISTS (SELECT 1 FROM summaries su"
                "                      WHERE su.analysis_run_id = a.run_id"
                "                        AND su.person_id = f.person_id)), -1)"
                f"{uw}", up,
            )
            # packets prepared (step 1) but not yet labeled + committed (step 2):
            # a run stays 'running' until `digest analyze commit` marks it done.
            pend = s.query_one(
                "SELECT COUNT(*) AS n FROM analysis_runs WHERE status = 'running'")

        active_f = (row["active_f"] or 0) + (row["passive_f"] or 0)
        return {
            "frames": row["frames"] or 0,
            "images_stored": row["images_stored"] or 0,
            "dup_linked": row["dup_linked"] or 0,
            "active_frames": row["active_f"] or 0,
            "passive_frames": row["passive_f"] or 0,
            "idle_frames": row["idle_f"] or 0,
            "work_minutes": round(active_f * interval / 60.0, 1),
            "idle_minutes": round((row["idle_f"] or 0) * interval / 60.0, 1),
            "days": row["days"] or 0,
            "apps": row["apps"] or 0,
            "first_ts": row["first_ts"],
            "last_ts": row["last_ts"],
            "sessions_analyzed": srow["sessions"] or 0,
            "frames_analyzed": srow["frames"] or 0,
            "low_value_sessions": srow["low_value"] or 0,
            "observations_total": obs["total"] or 0,
            "observations_open": obs["open_confirmed"] or 0,
            "reclaimable_minutes_per_week": round(obs["minutes"] or 0, 1),
            "unprocessed_frames": unrow["n"] or 0,
            "pending_runs": pend["n"] or 0,
        }

    def app_breakdown(
        self, person_id: Optional[str], ts_start: Optional[int],
        ts_end: Optional[int], limit: int = 12, department: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        interval = self.cfg.capture.interval_seconds
        fw, fp = self._window("ts_utc", person_id, ts_start, ts_end, department=department)
        with self._store() as s:
            rows = s.query(
                "SELECT app_name, COUNT(*) AS frames"
                " FROM frames"
                f" WHERE activity_state IN ('active','passive') AND app_name IS NOT NULL{fw}"
                " GROUP BY app_name ORDER BY frames DESC LIMIT ?",
                fp + [limit],
            )
        return [
            {"app": r["app_name"], "frames": r["frames"],
             "minutes": round(r["frames"] * interval / 60.0, 1)}
            for r in rows
        ]

    def daily_activity(
        self, person_id: Optional[str], ts_start: Optional[int], ts_end: Optional[int],
        department: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        interval = self.cfg.capture.interval_seconds
        fw, fp = self._window("ts_utc", person_id, ts_start, ts_end, department=department)
        with self._store() as s:
            rows = s.query(
                "SELECT local_day,"
                "  SUM(CASE WHEN activity_state IN ('active','passive') THEN 1 ELSE 0 END) AS work_f,"
                "  SUM(CASE WHEN activity_state='idle' THEN 1 ELSE 0 END) AS idle_f,"
                "  SUM(CASE WHEN screenshot_path IS NOT NULL AND dup_of IS NULL THEN 1 ELSE 0 END) AS images"
                f" FROM frames WHERE 1=1{fw}"
                " GROUP BY local_day ORDER BY local_day", fp,
            )
        return [
            {"day": r["local_day"], "work_minutes": round((r["work_f"] or 0) * interval / 60.0, 1),
             "idle_minutes": round((r["idle_f"] or 0) * interval / 60.0, 1),
             "images": r["images"] or 0}
            for r in rows
        ]

    # -- analysis runs ----------------------------------------------------
    def runs(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._store() as s:
            rows = s.query(
                "SELECT a.run_id, a.ts_start, a.ts_end, a.model, a.started_at,"
                "       a.finished_at, a.status,"
                "       (SELECT COUNT(*) FROM summaries su WHERE su.analysis_run_id=a.run_id) AS summaries"
                " FROM analysis_runs a ORDER BY a.started_at DESC LIMIT ?", (limit,),
            )
        return [dict(r) for r in rows]

    def run_detail(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._store() as s:
            run = s.query_one("SELECT * FROM analysis_runs WHERE run_id = ?", (run_id,))
            if run is None:
                return None
            summaries = s.query(
                "SELECT id, app_name, activity, category, capability, task,"
                "       low_value, friction, input_density, frame_count, ts_start, ts_end"
                " FROM summaries WHERE analysis_run_id = ? ORDER BY ts_start", (run_id,),
            )
        return {"run": dict(run), "summaries": [dict(r) for r in summaries]}

    # -- suggestions (the improvement report) ----------------------------
    def report(
        self, person_id: Optional[str], department: Optional[str] = None
    ) -> dict[str, Any]:
        with self._store() as s:
            return build_report(
                self.cfg, s, person_id=person_id, department=department,
                statuses=list(DEFAULT_STATUSES))

    # -- digests ----------------------------------------------------------
    def digests(
        self, person_id: Optional[str], limit: int = 25, department: Optional[str] = None
    ) -> list[dict[str, Any]]:
        with self._store() as s:
            rows = s.list_digests(person_id=person_id, department=department, limit=limit)
        return [
            {"id": r["id"], "short_id": r["id"][:8], "person_id": r["person_id"],
             "person": r["display_name"], "department": r["department"],
             "period": r["period"], "generated_at": r["generated_at"],
             "generated_day": clock.local_day(r["generated_at"]) if r["generated_at"] else None,
             "token_cost": r["token_cost"]}
            for r in rows
        ]

    def digest_detail(self, digest_id: str) -> Optional[dict[str, Any]]:
        with self._store() as s:
            rows = s.find_digest(digest_id)
            if not rows or len(rows) > 1:
                return None
            r = rows[0]
            return {"id": r["id"], "period": r["period"],
                    "generated_at": r["generated_at"],
                    "content_md": r["content_md"]}
