"""Background job runner for the dashboard's trigger buttons.

Only the **model-free** parts of the pipeline can be launched unattended, so those
are all this exposes:
  * `rollup`  — (re)build app_usage_rollup from frame metadata;
  * `prepare` — sessionize + mine unanalyzed frames, open an analysis_run, write a
    vision packet. The vision labeling + `commit` still happen in Claude Code
    (that is the one step that needs a model), so a prepare job finishes by
    reporting the packet path and the exact next command.
  * `digest`  — compile the opportunity data report from the current ledger and
    save it (a deterministic ranked table — key-less; the AI judgment it ranks
    was written at commit time);
  * `brief`   — bundle the ledger + period stats into an executive-brief packet.
    Like `prepare`, the model half stays in Claude Code (it writes the narrative
    Markdown, then `digest brief commit` saves it), so this job finishes by
    reporting the packet path and what to tell Claude.
  * `compile` — the dashboard's one post-labeling button: runs `digest` then
    `brief` back to back (both model-free), so a single click saves the ranked
    data report AND prepares the brief packet. Finishes by reporting the saved
    report and what to tell Claude to write the narrative.

Each job runs in a daemon thread with its own `Store`. State is kept in memory
(this is a single-process local dashboard); a restart clears history, which is
fine for the beta.
"""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable, Optional

from .. import clock
from ..config import Config
from ..db.store import Store

JOB_KINDS = ("rollup", "prepare", "digest", "brief", "compile")


class Job:
    __slots__ = ("id", "kind", "params", "status", "created_at", "finished_at",
                 "result", "error", "message")

    def __init__(self, job_id: str, kind: str, params: dict[str, Any]):
        self.id = job_id
        self.kind = kind
        self.params = params
        self.status = "running"           # running | done | failed
        self.created_at = clock.now_ms()
        self.finished_at: Optional[int] = None
        self.result: Optional[dict[str, Any]] = None
        self.error: Optional[str] = None
        self.message = f"{kind} started"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "created_at": self.created_at, "finished_at": self.finished_at,
            "result": self.result, "error": self.error, "message": self.message,
        }


class JobManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # -- registry ---------------------------------------------------------
    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [j.to_dict() for j in jobs[:limit]]

    # -- launch -----------------------------------------------------------
    def start(self, kind: str, params: dict[str, Any]) -> Job:
        if kind not in JOB_KINDS:
            raise ValueError(f"unknown job kind: {kind!r}")
        job = Job(_new_id(), kind, params)
        with self._lock:
            self._jobs[job.id] = job
        runner = _RUNNERS[kind]
        t = threading.Thread(target=self._run, args=(job, runner), daemon=True)
        t.start()
        return job

    def _run(self, job: Job, runner: Callable[[Config, Store, dict], dict]) -> None:
        try:
            with Store(self.cfg.db_path) as store:
                store.init_db()
                result = runner(self.cfg, store, job.params)
            job.result = result
            job.message = result.get("message", f"{job.kind} done")
            job.status = "done"
        except Exception as exc:  # surface any failure to the UI, never crash the server
            job.error = f"{type(exc).__name__}: {exc}"
            job.message = job.error
            job.status = "failed"
            traceback.print_exc()
        finally:
            job.finished_at = clock.now_ms()


def _new_id() -> str:
    from ..db.store import new_id

    return new_id()


# -- job implementations (model-free) -------------------------------------
def _resolve_person(store: Store, person_id: Optional[str]) -> dict:
    if person_id:
        p = store.get_person(person_id)
        if not p:
            raise ValueError(f"no person with id {person_id}")
        return dict(p)
    p = store.first_person()
    if not p:
        raise ValueError("no people yet — add a person before running analysis")
    return dict(p)


def _job_rollup(cfg: Config, store: Store, params: dict) -> dict:
    from ..rollup import build_rollup

    n = build_rollup(cfg, store)
    return {"rows": n, "message": f"rollup upserted {n} (person, device, day, app) rows"}


def _job_prepare(cfg: Config, store: Store, params: dict) -> dict:
    from ..analysis.prepare import prepare

    p = _resolve_person(store, params.get("person_id"))
    info = prepare(
        cfg, store, p["person_id"],
        all_frames=bool(params.get("all_frames")),
        model=params.get("model"),
    )
    if info["frame_count"] == 0:
        return {"frame_count": 0,
                "message": "no unanalyzed work frames in range — nothing to prepare "
                           "(record a session, or re-run with 'all frames')."}
    info["message"] = (
        f"prepared run {info['run_id'][:8]}: {info['frame_count']} frames -> "
        f"{info['session_count']} sessions bundled into a packet. Step 2 is next "
        "— open Claude Code and say:"
    )
    info["say_to_claude"] = "Label and commit the prepared analysis packet."
    return info


def _job_digest(cfg: Config, store: Store, params: dict) -> dict:
    from ..digest import generate_digest, save_digest

    p = _resolve_person(store, params.get("person_id"))
    digest = generate_digest(
        cfg, store,
        period=params.get("period"),
        person_id=p["person_id"],
        min_minutes=params.get("min_minutes"),
    )
    saved_id = save_digest(store, digest)
    n = digest["report"]["totals"]["opportunity_count"]
    return {
        "digest_id": saved_id,
        "short_id": saved_id[:8],
        "period": digest["period"],
        "opportunity_count": n,
        "content_md": digest["content_md"],
        "message": f"generated digest {saved_id[:8]} ({n} opportunities, "
                   f"period={digest['period']})",
    }


def _job_brief(cfg: Config, store: Store, params: dict) -> dict:
    from ..brief import prepare_brief

    p = _resolve_person(store, params.get("person_id"))
    info = prepare_brief(
        cfg, store, p["person_id"], period=params.get("period") or "7d")
    if info["opportunity_count"] == 0:
        return {"opportunity_count": 0,
                "message": "the opportunity ledger is empty — run steps 1-3 "
                           "first, then prepare the brief."}
    info["say_to_claude"] = "Write the executive brief from the prepared brief packet."
    info["message"] = (
        f"brief packet ready ({info['opportunity_count']} opportunities, "
        f"period={info['period']}). Open Claude Code and say:")
    return info


def _job_compile(cfg: Config, store: Store, params: dict) -> dict:
    """Data report + brief packet in one shot — both model-free, run in sequence.

    The digest is generated with its own cadence label (not the dashboard's
    window period, which is for the brief's activity stats), so it reads e.g.
    'weekly', not '7d'."""
    from ..brief import prepare_brief
    from ..digest import generate_digest, save_digest

    p = _resolve_person(store, params.get("person_id"))
    # 1) ranked data report — always saved (cheap, and it's the receipts behind
    #    the narrative). Uses the configured cadence label, not the UI window.
    digest = generate_digest(
        cfg, store, period=None, person_id=p["person_id"],
        min_minutes=params.get("min_minutes"))
    saved_id = save_digest(store, digest)
    n = digest["report"]["totals"]["opportunity_count"]
    result: dict[str, Any] = {
        "digest_id": saved_id, "short_id": saved_id[:8],
        "opportunity_count": n,
    }
    if n == 0:
        result["message"] = (
            "data report saved (0 opportunities yet) — capture work and run "
            "steps 1–2 first; nothing to write a brief about yet.")
        return result
    # 2) brief packet — scoped to the dashboard's Period selector
    brief = prepare_brief(
        cfg, store, p["person_id"], period=params.get("period") or "7d")
    result["brief_packet"] = brief["packet_path"]
    result["say_to_claude"] = "Write the executive brief from the prepared brief packet."
    result["message"] = (
        f"data report saved ({n} opportunit{'y' if n == 1 else 'ies'}) and brief "
        f"packet ready (period={brief['period']}). For the written brief, open "
        "Claude Code and say:")
    return result


_RUNNERS: dict[str, Callable[[Config, Store, dict], dict]] = {
    "rollup": _job_rollup,
    "prepare": _job_prepare,
    "digest": _job_digest,
    "brief": _job_brief,
    "compile": _job_compile,
}
