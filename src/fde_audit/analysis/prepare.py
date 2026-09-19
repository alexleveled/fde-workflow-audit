"""Build an analysis packet (model-free) for Claude Code to label with vision.

`prepare` runs sessionization + transition mining over unanalyzed work frames,
opens an analysis_run (status=running), and writes a JSON packet describing:
  * the window and the mining signals (ping-pong, rituals, bigrams, top apps);
  * one entry per window-session with the representative screenshots to read and
    an empty `label` object for Claude to fill;
  * the controlled vocabularies the labels must use.

Claude then reads each session's `representative_frames[*].path_abs` with vision,
fills the `label` (and optionally adds `opportunities`), and passes the SAME file
to `digest analyze commit`. The packet round-trips: prepared -> labeled ->
committed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .. import clock
from ..config import Config
from ..db.store import Store
from . import taxonomy
from .mining import mine
from .sessionize import Session, sessionize

PACKET_VERSION = 2


def _session_json(cfg: Config, s: Session) -> dict:
    reps = []
    for r in s.representatives:
        abs_path = (cfg.data_dir / r.path).resolve()
        reps.append({
            "frame_id": r.frame_id,
            "position": r.position,
            "path_rel": r.path,
            "path_abs": str(abs_path),
            "exists": abs_path.exists(),
        })
    return {
        "index": s.index,
        "app_name": s.app_name,
        "window_titles": s.window_titles,
        "url_domains": s.url_domains,
        "local_day": s.local_day,
        "ts_start": s.ts_start,
        "ts_end": s.ts_end,
        "frame_id_start": s.frame_id_start,
        "frame_id_end": s.frame_id_end,
        "frame_count": s.frame_count,
        "minutes": round(s.work_seconds / 60.0, 1),
        "input_density": s.input_density,
        "interaction_mode": "input-heavy" if s.input_density >= 0.5 else "reading",
        "boundary_reason": s.boundary_reason,
        "representative_frames": reps,
        # Claude fills this in; commit validates it.
        "label": {
            "capability": None,          # REQUIRED — one of packet.capabilities
            "task": None,                # free text: the specific thing
            "activity": None,            # short free-text description
            "category": None,            # optional coarse bucket (free text)
            "low_value": None,           # true/false
            "friction": None,            # null or one of packet.friction_kinds
        },
    }


INSTRUCTIONS = (
    "GOAL: find EVERY plausible way to economize this person's workflow — this is "
    "open-ended. The `mining` block (ping-pong, rituals, interaction_modes) is only "
    "a set of cheap hints at hotspots; it is NOT the list of things to look for, "
    "and most opportunities will NOT come from it. Judge from what the screenshots "
    "actually show.\n"
    "\n"
    "STEP 1 — label every session: open every representative_frames[*].path_abs "
    "with vision and fill its `label`. `capability` MUST be one of `capabilities`; "
    "`friction` is null or one of `friction_kinds`.\n"
    "\n"
    "STEP 2 — add typed entries to the top-level `opportunities` array for anything "
    "you can justify, across the FULL range of `opportunity_types`. Look for at "
    "least all of:\n"
    "  * automate — any repeated manual sequence a script/macro/shortcut could do;\n"
    "  * integrate — two systems kept in sync by hand (copy-paste, re-typing, "
    "manual export/import); ping-pong is one signal but not the only one;\n"
    "  * consolidate — the SAME `capability` handled across multiple apps (compare "
    "`mining.top_apps` and the capabilities you assign); a license-cut candidate;\n"
    "  * build-custom — a bespoke small app/extension that would collapse the work;\n"
    "  * train — the tool can already do it faster: an unused hotkey/feature, doing "
    "by hand what a menu/formula/filter does, slow navigation, redundant clicks;\n"
    "  * eliminate — low-value or avoidable activity (idle browsing, rework, "
    "waiting on slow loads, needless context-switching).\n"
    "Also weigh single-app inefficiency (not just cross-app switching): a lot of "
    "time in one tool doing something tedious is itself an opportunity.\n"
    "Each opportunity: {opportunity_type (from `opportunity_types`), pattern_key, "
    "description, suggested_upgrade, optionally est_minutes_per_week}.\n"
    "\n"
    "STEP 3 — run `digest analyze commit --packet <this file>`."
)


def prepare(
    cfg: Config,
    store: Store,
    person_id: str,
    *,
    since: Optional[int] = None,
    until: Optional[int] = None,
    all_frames: bool = False,
    out_path: Optional[Path] = None,
    model: Optional[str] = None,
) -> dict:
    """Compute sessions + mining, open a run, and write the packet. Returns a
    small summary dict (paths, counts, run_id)."""
    ac = cfg.analysis
    if since is None and not all_frames:
        wm = store.analysis_watermark(person_id)
        # watermark is the last analyzed frame's ts (inclusive); start just past it.
        since = wm + 1 if wm is not None else None

    frames = store.work_frames(person_id, ts_start=since, ts_end=until)
    if not frames:
        # Nothing to analyze — don't open a run that would strand as 'running'.
        return {
            "run_id": None, "packet_path": None, "frame_count": 0,
            "session_count": 0, "pingpong": 0, "rituals": 0,
            "ts_start": None, "ts_end": None,
        }
    sessions = sessionize(
        frames,
        interval_seconds=cfg.capture.interval_seconds,
        max_minutes=ac.session_max_minutes,
        idle_gap_seconds=ac.session_idle_gap_seconds,
        hash_jump_bits=ac.session_hash_jump_bits,
        long_session_minutes=ac.long_session_minutes,
        max_titles=ac.max_titles_per_session,
    )
    mining = mine(
        sessions,
        pingpong_min_switches=ac.pingpong_min_switches,
        ngram_top=ac.ngram_top,
        ritual_min_days=ac.ritual_min_days,
    )

    ts_start = frames[0]["ts_utc"] if frames else None
    ts_end = frames[-1]["ts_utc"] if frames else None
    now = clock.now_ms()
    run_id = store.start_analysis_run(
        started_at=now, ts_start=ts_start, ts_end=ts_end, model=model,
    )

    packet = {
        "packet_version": PACKET_VERSION,
        "run_id": run_id,
        "person_id": person_id,
        "generated_at": now,
        "model": model,
        "window": {
            "ts_start": ts_start,
            "ts_end": ts_end,
            "frame_count": len(frames),
            "session_count": len(sessions),
            "since_watermark": since,
        },
        "capabilities": list(taxonomy.CAPABILITIES),
        "opportunity_types": list(taxonomy.OPPORTUNITY_TYPES),
        "friction_kinds": list(taxonomy.FRICTION_KINDS),
        "mining": mining.to_dict(),
        "sessions": [_session_json(cfg, s) for s in sessions],
        "opportunities": [],   # Claude appends typed opportunities here
        "instructions": INSTRUCTIONS,
    }

    if out_path is None:
        out_dir = cfg.data_dir / "analysis"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"packet_{run_id}.json"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")

    return {
        "run_id": run_id,
        "packet_path": str(out_path),
        "frame_count": len(frames),
        "session_count": len(sessions),
        "pingpong": len(mining.pingpong),
        "rituals": len(mining.rituals),
        "ts_start": ts_start,
        "ts_end": ts_end,
    }
