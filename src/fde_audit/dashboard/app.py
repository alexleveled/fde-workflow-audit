"""FastAPI application for the beta dashboard.

Serves a static frontend (`static/index.html` + `styles.css` + `app.js`, no
external assets or build step) and a small JSON API over `DashboardService`
(reads) and `JobManager` (triggers). The app is created per `Config` so it
always points at the same SQLite DB the CLI uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import Config
from ..monitor import StatusMonitor
from .jobs import JobManager
from .service import DashboardService, resolve_window

_STATIC = Path(__file__).with_name("static")


def create_app(cfg: Config):
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

    app = FastAPI(title="FDE Audit — Dashboard", docs_url="/api/docs")
    svc = DashboardService(cfg)
    jobs = JobManager(cfg)
    status_monitor = StatusMonitor(cfg)

    @app.on_event("startup")
    def _start_monitor() -> None:
        # Sweeps liveness on a timer so the status log records up/down changes
        # even when no browser is watching.
        status_monitor.start()

    @app.on_event("shutdown")
    def _stop_monitor() -> None:
        status_monitor.stop()

    def _person(person: Optional[str]) -> Optional[str]:
        # "" or "all" from the UI -> None (every person); otherwise the id as-is.
        if person in (None, "", "all"):
            return None
        return person

    def _dept(department: Optional[str]) -> Optional[str]:
        # "" or "all" from the UI -> None (every department); otherwise as-is.
        if department in (None, "", "all"):
            return None
        return department

    # -- page -------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    # -- live change stream (SSE) -----------------------------------------
    @app.get("/api/events")
    async def api_events():
        """Server-Sent Events feed that pushes a `db-changed` event whenever the
        SQLite database is written by *any other* connection — the capture
        recorder landing frames, an analysis job committing, or the status
        monitor logging an up/down change.

        Detection is `PRAGMA data_version`: a counter SQLite bumps on every
        commit made outside this connection. Reading it is an in-memory op (no
        query, no lock), so polling it once a second costs effectively nothing —
        the browser holds one idle connection and the server does one cheap read
        per tick. The frontend turns each event into a soft, in-place refresh of
        the active view; no full page reload.

        Disconnect is handled by cancellation: when the browser closes the
        stream, the ASGI server cancels this generator, the CancelledError
        unwinds through the `await`, and the `finally` closes the connection.

        When the backend moves to Postgres, this same endpoint can swap the poll
        loop for LISTEN/NOTIFY (true push) and the browser side is unchanged.
        """
        import asyncio
        import sqlite3

        # A dedicated read-only connection in autocommit mode (isolation_level=
        # None) so every PRAGMA sees the latest committed data_version instead of
        # a held snapshot. Lives for the life of the stream, closed on disconnect.
        conn = sqlite3.connect(
            str(cfg.db_path), isolation_level=None, check_same_thread=False
        )

        def data_version() -> int:
            return conn.execute("PRAGMA data_version").fetchone()[0]

        async def stream():
            try:
                last = data_version()
                # advise the client's reconnect backoff, then announce we're live
                yield "retry: 3000\n\n"
                yield "event: hello\ndata: {}\n\n"
                idle_ticks = 0
                while True:
                    await asyncio.sleep(1.0)
                    try:
                        cur = data_version()
                    except sqlite3.Error:
                        break
                    if cur != last:
                        last = cur
                        idle_ticks = 0
                        yield "event: db-changed\ndata: {}\n\n"
                    else:
                        # heartbeat comment every ~20s so idle connections (and any
                        # proxy in between) stay open and disconnects surface fast
                        idle_ticks += 1
                        if idle_ticks >= 20:
                            idle_ticks = 0
                            yield ": keep-alive\n\n"
            finally:
                conn.close()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- reads ------------------------------------------------------------
    @app.get("/api/live")
    def api_live():
        return svc.live_capture()

    @app.get("/api/status-events")
    def api_status_events(limit: int = 50):
        return {"events": svc.status_events(limit=limit)}

    @app.get("/api/people")
    def api_people():
        return {"people": svc.people(), "departments": svc.departments(),
                "default": svc.default_person_id()}

    @app.get("/api/overview")
    def api_overview(
        person: Optional[str] = None, department: Optional[str] = None,
        period: Optional[str] = "7d",
        ts_from: Optional[int] = Query(None), ts_to: Optional[int] = Query(None),
    ):
        ts_start, ts_end, label = resolve_window(period, ts_from, ts_to)
        pid, dept = _person(person), _dept(department)
        return {
            "scope": {"person_id": pid, "department": dept, "period": label,
                      "ts_start": ts_start, "ts_end": ts_end},
            "overview": svc.overview(pid, ts_start, ts_end, department=dept),
            "apps": svc.app_breakdown(pid, ts_start, ts_end, department=dept),
            "daily": svc.daily_activity(pid, ts_start, ts_end, department=dept),
        }

    @app.get("/api/runs")
    def api_runs(limit: int = 25):
        return {"runs": svc.runs(limit=limit)}

    @app.get("/api/runs/{run_id}")
    def api_run_detail(run_id: str):
        detail = svc.run_detail(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="run not found")
        return detail

    @app.get("/api/report")
    def api_report(person: Optional[str] = None, department: Optional[str] = None):
        return svc.report(_person(person), department=_dept(department))

    @app.get("/api/digests")
    def api_digests(
        person: Optional[str] = None, department: Optional[str] = None, limit: int = 25
    ):
        return {"digests": svc.digests(_person(person), limit=limit,
                                       department=_dept(department))}

    @app.get("/api/digests/{digest_id}")
    def api_digest_detail(digest_id: str):
        d = svc.digest_detail(digest_id)
        if d is None:
            raise HTTPException(status_code=404, detail="digest not found (or ambiguous id)")
        return d

    # -- triggers ---------------------------------------------------------
    @app.post("/api/actions/{kind}")
    def api_action(kind: str, body: Optional[dict] = None):
        body = body or {}
        params = {
            "person_id": _person(body.get("person")),
            "all_frames": bool(body.get("all_frames")),
            "period": body.get("period"),
            "model": body.get("model"),
        }
        try:
            job = jobs.start(kind, params)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return JSONResponse(job.to_dict(), status_code=202)

    @app.get("/api/jobs")
    def api_jobs(limit: int = 25):
        return {"jobs": jobs.list(limit=limit)}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job.to_dict()

    # -- static assets (styles.css / app.js) -------------------------------
    from fastapi.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    return app


def run(cfg: Config, host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn

    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level="info")
