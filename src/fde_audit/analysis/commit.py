"""Commit a labeled analysis packet — one transaction (Phase 2).

Reads the packet that `prepare` wrote and Claude labeled, validates every label
against the controlled vocabularies, then in a SINGLE transaction:
  * inserts one `summaries` row per session,
  * upserts the `observations` ledger from mining signals + labels + any explicit
    `opportunities`,
  * marks the analysis_run `done`.

If anything is invalid the whole thing rolls back and the run is marked `failed`,
so a bad pass strands nothing (the watermark only advances on a done run).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .. import clock
from ..config import Config
from ..db.store import Store
from . import taxonomy

# friction -> the opportunity it usually implies
_FRICTION_OPP = {
    "error-dialog": "train",
    "loading-wait": "eliminate",
    "repeated-search": "train",
    "manual-copy": "integrate",
    "context-switch": "consolidate",
    "rework": "automate",
}


class CommitError(ValueError):
    """Raised on an invalid packet; the caller marks the run failed."""


def _weekly(observed_minutes: float, window: dict) -> float:
    """Extrapolate observed minutes to a per-week figure from the analyzed span.
    Floors the span at 1 day so a short recording can't inflate the estimate
    beyond 7x — a documented lower-confidence bound, not vibes."""
    ts0, ts1 = window.get("ts_start"), window.get("ts_end")
    if not ts0 or not ts1 or ts1 <= ts0:
        return round(observed_minutes, 1)
    span_days = max((ts1 - ts0) / 86_400_000.0, 1.0)
    return round(observed_minutes * 7.0 / span_days, 1)


def _validate_and_build_summaries(packet: dict, run_id: str, person_id: str) -> list[dict]:
    rows: list[dict] = []
    missing: list[int] = []
    model = packet.get("model") or "claude-code"
    for s in packet.get("sessions", []):
        label = s.get("label") or {}
        cap = label.get("capability")
        if not cap:
            missing.append(s.get("index"))
            continue
        capability = taxonomy.validate_capability(cap)
        friction = label.get("friction")
        if friction is not None and friction not in taxonomy.FRICTION_KINDS:
            raise CommitError(
                f"session {s.get('index')}: friction {friction!r} not in "
                f"{', '.join(taxonomy.FRICTION_KINDS)}"
            )
        rows.append({
            "analysis_run_id": run_id,
            "person_id": person_id,
            "frame_id_start": s["frame_id_start"],
            "frame_id_end": s["frame_id_end"],
            "ts_start": s["ts_start"],
            "ts_end": s["ts_end"],
            "frame_count": s["frame_count"],
            "activity": label.get("activity"),
            "category": label.get("category"),
            "app_name": s.get("app_name"),
            "low_value": 1 if label.get("low_value") else 0,
            "vision_model": model,
            "logger_model": None,
            "tokens": None,
            "capability": capability,
            "task": label.get("task"),
            "input_density": s.get("input_density"),
            "friction": friction,
            "boundary_reason": s.get("boundary_reason"),
        })
    if missing:
        raise CommitError(
            f"sessions missing a required `capability` label: {missing}. "
            "Every session must be labeled before commit."
        )
    return rows


def _derive_observations(packet: dict, summary_rows: list[dict]) -> list[dict]:
    """Turn mining signals + labels into typed, priced observation candidates.
    Each returned dict is ready for store.upsert_observation."""
    window = packet.get("window", {})
    mining = packet.get("mining", {})
    obs: list[dict] = []

    # 1) ping-pong loops -> integrate (manual data transfer between two apps)
    for pp in mining.get("pingpong", []):
        a, b = pp["app_a"], pp["app_b"]
        key_pair = " <-> ".join(sorted([a, b]))
        obs.append({
            "pattern_key": f"pingpong:{key_pair}",
            "description": f"Rapid alternation between {a} and {b} "
                           f"({pp['switches']} switches) — likely manual data transfer.",
            "category": "workflow",
            "opportunity_type": "integrate",
            "occurrences": pp["switches"],
            "est_minutes_per_week": _weekly(pp.get("est_minutes", 0), window),
            "suggested_upgrade": f"Connect {a} and {b} (integration/sync) or build a "
                                 f"small bridge so data doesn't move by hand.",
            "first_seen": pp.get("ts_start"),
            "last_seen": pp.get("ts_end"),
        })

    # 2) recurring rituals -> automate (a fixed multi-app routine)
    for r in mining.get("rituals", []):
        seq = " -> ".join(r["sequence"])
        obs.append({
            "pattern_key": f"ritual:{seq}",
            "description": f"Recurring sequence {seq} on {r['day_count']} day(s) — a routine.",
            "category": "routine",
            "opportunity_type": "automate",
            "occurrences": r["day_count"],
            "est_minutes_per_week": None,
            "suggested_upgrade": f"Script the {seq} routine end to end.",
            "first_seen": None,
            "last_seen": None,
        })

    # 3) labeled low-value sessions -> eliminate, grouped by (app, capability)
    lowval: dict[tuple, dict] = {}
    interval_min = None  # minutes derived from frame_count via packet sessions
    minutes_by_index = {s["index"]: s.get("minutes", 0) for s in packet.get("sessions", [])}
    idx_by_frames = {s["frame_id_start"]: s["index"] for s in packet.get("sessions", [])}
    for row in summary_rows:
        if not row["low_value"]:
            continue
        key = (row["app_name"], row["capability"])
        idx = idx_by_frames.get(row["frame_id_start"])
        mins = minutes_by_index.get(idx, 0)
        g = lowval.setdefault(key, {"count": 0, "minutes": 0.0,
                                    "first": row["ts_start"], "last": row["ts_end"]})
        g["count"] += 1
        g["minutes"] += mins
        g["first"] = min(g["first"], row["ts_start"])
        g["last"] = max(g["last"], row["ts_end"])
    for (app, cap), g in lowval.items():
        obs.append({
            "pattern_key": f"lowvalue:{app}:{cap}",
            "description": f"Low-value time in {app} ({cap}) across {g['count']} session(s).",
            "category": cap,
            "opportunity_type": "eliminate",
            "occurrences": g["count"],
            "est_minutes_per_week": _weekly(g["minutes"], window),
            "suggested_upgrade": f"Reduce or batch {cap} time in {app}.",
            "first_seen": g["first"],
            "last_seen": g["last"],
        })

    # 4) friction flags -> mapped opportunity, grouped by (app, friction)
    friction_g: dict[tuple, dict] = {}
    for row in summary_rows:
        if not row["friction"]:
            continue
        key = (row["app_name"], row["friction"])
        g = friction_g.setdefault(key, {"count": 0, "first": row["ts_start"], "last": row["ts_end"]})
        g["count"] += 1
        g["first"] = min(g["first"], row["ts_start"])
        g["last"] = max(g["last"], row["ts_end"])
    for (app, fr), g in friction_g.items():
        obs.append({
            "pattern_key": f"friction:{app}:{fr}",
            "description": f"{fr} friction in {app} across {g['count']} session(s).",
            "category": "friction",
            "opportunity_type": _FRICTION_OPP.get(fr, "train"),
            "occurrences": g["count"],
            "est_minutes_per_week": None,
            "suggested_upgrade": f"Address {fr} in {app}.",
            "first_seen": g["first"],
            "last_seen": g["last"],
        })

    # 5) explicit opportunities Claude added to the packet
    for o in packet.get("opportunities", []):
        opp = taxonomy.validate_opportunity_type(o.get("opportunity_type"))
        obs.append({
            "pattern_key": o["pattern_key"],
            "description": o.get("description", ""),
            "category": o.get("category"),
            "opportunity_type": opp,
            "occurrences": int(o.get("occurrences", 1)),
            "est_minutes_per_week": o.get("est_minutes_per_week"),
            "suggested_upgrade": o.get("suggested_upgrade"),
            "first_seen": o.get("first_seen") or window.get("ts_start"),
            "last_seen": o.get("last_seen") or window.get("ts_end"),
        })
    return obs


def _derive_consolidation(cfg: Config, store: Store, person_id: str) -> list[dict]:
    """A `capability` handled across >= 2 distinct apps is a consolidation
    candidate (the license-cut signal). Computed over ALL of the person's
    summaries — including the rows just inserted this run — so it sharpens as more
    data accrues. occurrences = the app count (a snapshot; upserted with
    accumulate=False so it doesn't inflate on re-runs)."""
    interval = cfg.capture.interval_seconds
    spread: dict[str, dict] = {}
    for r in store.capability_app_spread(person_id):
        cap = r["capability"]
        g = spread.setdefault(cap, {"apps": {}, "first": r["first_seen"], "last": r["last_seen"]})
        g["apps"][r["app_name"]] = g["apps"].get(r["app_name"], 0) + (r["frames"] or 0)
        g["first"] = min(g["first"], r["first_seen"])
        g["last"] = max(g["last"], r["last_seen"])

    obs: list[dict] = []
    for cap, g in spread.items():
        apps = sorted(g["apps"], key=lambda a: g["apps"][a], reverse=True)
        if len(apps) < 2:
            continue
        total_minutes = sum(g["apps"].values()) * interval / 60.0
        obs.append({
            "pattern_key": f"consolidate:{cap}",
            "description": f"{cap} is split across {len(apps)} apps: {', '.join(apps)}.",
            "category": cap,
            "opportunity_type": "consolidate",
            "occurrences": len(apps),
            "est_minutes_per_week": None,   # consolidation value is license/context, not minutes saved
            "suggested_upgrade": f"Standardize {cap} on one tool (currently {', '.join(apps)}) "
                                 "to cut overlap and license cost.",
            "first_seen": g["first"],
            "last_seen": g["last"],
            "accumulate": False,
        })
    return obs


def commit(cfg: Config, store: Store, packet_path: str | Path) -> dict:
    """Validate + write a labeled packet in one transaction. Returns counts.
    On any error the run is marked failed and nothing else persists."""
    packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
    run_id = packet["run_id"]
    person_id = packet["person_id"]

    run = store.get_analysis_run(run_id)
    if run is None:
        raise CommitError(f"no analysis_run {run_id} — was the packet prepared?")
    if run["status"] == "done":
        raise CommitError(f"run {run_id} already committed (status=done)")

    now = clock.now_ms()
    try:
        summary_rows = _validate_and_build_summaries(packet, run_id, person_id)
        observations = _derive_observations(packet, summary_rows)

        for row in summary_rows:
            store.insert_summary(row)
        # consolidation is derived AFTER this run's summaries are inserted, so it
        # sees the full cumulative capability/app spread.
        observations += _derive_consolidation(cfg, store, person_id)
        for o in observations:
            store.upsert_observation(
                person_id=person_id,
                pattern_key=o["pattern_key"],
                description=o["description"],
                category=o.get("category"),
                opportunity_type=o.get("opportunity_type"),
                est_minutes_per_week=o.get("est_minutes_per_week"),
                suggested_upgrade=o.get("suggested_upgrade"),
                first_seen=o.get("first_seen") or now,
                last_seen=o.get("last_seen") or now,
                occurrences=o.get("occurrences", 1),
                confirm_threshold=cfg.analysis.observation_confirm,
                accumulate=o.get("accumulate", True),
            )
        store.finish_analysis_run(run_id, finished_at=now, status="done")
        store.commit()   # single commit — summaries + observations + run together
    except Exception:
        store.conn.rollback()
        store.fail_analysis_run(run_id, finished_at=now)
        raise

    return {
        "run_id": run_id,
        "summaries": len(summary_rows),
        "observations": len(observations),
    }
