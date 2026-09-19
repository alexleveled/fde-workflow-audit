"""The executive brief — the narrative, model-written report (via Claude Code).

The Phase 4 digest is a *data report*: a deterministic ranked table over the
observations ledger. It never writes prose. The **executive brief** is the
narrative layer on top of it — themes, tradeoffs, "build this custom app",
"cut these licenses" — and that judgment call needs a model. In the beta the
model is **Claude Code** (no API key), so the brief round-trips the same way
the vision pass does:

  1. `prepare_brief` (model-free) — bundles everything Claude needs into one
     JSON packet: window activity stats, top apps, and the full ranked report
     (`reporting.build_report`). No screenshots — the brief reads the ledger,
     not pixels.
  2. Claude Code reads the packet, writes the narrative Markdown to the
     packet's `output_md_path`.
  3. `commit_brief` — validates the Markdown exists and is non-trivial, then
     saves it to the `digests` table with a `exec-<period>` period label, so
     it shows up next to the data reports in the dashboard's "Saved reports".

Activity stats are scoped to the selected period; the opportunity report is
the cumulative ledger (evidence doesn't expire when the window moves).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from . import clock
from .config import Config
from .db.store import Store, new_id
from .reporting import DEFAULT_STATUSES, build_report

BRIEF_VERSION = 1

# dashboard period label -> lookback in days (None = all time)
PERIOD_DAYS: dict[str, Optional[int]] = {
    "1d": 1, "7d": 7, "30d": 30, "90d": 90, "all": None,
}

INSTRUCTIONS = (
    "You are writing the EXECUTIVE BRIEF for this person's work period — the "
    "narrative report a manager would actually read. The `report` block is the "
    "ranked, priced opportunity ledger the analysis pass already produced; the "
    "`activity` block is what the period looked like (time, apps, days). Do NOT "
    "invent numbers — every figure you cite must come from this packet.\n"
    "\n"
    "Write Markdown with roughly this shape:\n"
    "  1. A 2-3 sentence headline: what this period looked like and the total "
    "reclaimable time on the table.\n"
    "  2. The 2-4 THEMES behind the opportunities (not a row-by-row restatement "
    "of the table — synthesize: what do the top items have in common?).\n"
    "  3. Concrete recommendations, in priority order, including where justified: "
    "custom apps/scripts worth building (build-custom / automate rows), licenses "
    "or apps to cut (the consolidation candidates), and integrations to set up. "
    "For each: what to do, what it saves per week, and rough effort.\n"
    "  4. What is NOT worth acting on yet (thin evidence, small savings) and why.\n"
    "  5. Suggested next steps for the coming period.\n"
    "\n"
    "Keep it under ~2 pages of Markdown. Plain confident prose; no filler.\n"
    "\n"
    "THEN: write the finished Markdown to `output_md_path` (exactly that path), "
    "and run `commit_command` to save it as a digest. Do not edit this packet."
)


def _window(period: str) -> tuple[Optional[int], Optional[int]]:
    days = PERIOD_DAYS.get(period)
    if days is None:
        return None, None
    now = clock.now_ms()
    return now - days * 86_400_000, now


def prepare_brief(
    cfg: Config,
    store: Store,
    person_id: str,
    *,
    period: str = "7d",
    out_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Bundle activity stats + the ranked report into a brief packet for Claude
    Code to write from. Model-free. Returns a summary dict (paths, counts, and
    the exact thing to tell Claude)."""
    p = store.get_person(person_id)
    if not p:
        raise ValueError(f"no person with id {person_id}")

    report = build_report(
        cfg, store, person_id=person_id, statuses=list(DEFAULT_STATUSES))
    n_opps = report["totals"]["opportunity_count"]
    if n_opps == 0:
        # nothing to narrate — don't write a packet Claude can't do anything with
        return {"brief_id": None, "packet_path": None, "output_md_path": None,
                "period": period, "opportunity_count": 0}

    ts_start, ts_end = _window(period)
    interval = cfg.capture.interval_seconds
    where, params = " WHERE person_id = ?", [person_id]
    if ts_start is not None:
        where += " AND ts_utc >= ?"
        params.append(ts_start)
    if ts_end is not None:
        where += " AND ts_utc <= ?"
        params.append(ts_end)
    row = store.query_one(
        "SELECT COUNT(*) AS frames,"
        "  SUM(CASE WHEN activity_state IN ('active','passive') THEN 1 ELSE 0 END) AS work_f,"
        "  SUM(CASE WHEN activity_state='idle' THEN 1 ELSE 0 END) AS idle_f,"
        "  COUNT(DISTINCT local_day) AS days, COUNT(DISTINCT app_name) AS apps"
        f" FROM frames{where}", params)
    apps = store.query(
        "SELECT app_name, COUNT(*) AS frames FROM frames"
        f"{where} AND activity_state IN ('active','passive') AND app_name IS NOT NULL"
        " GROUP BY app_name ORDER BY frames DESC LIMIT 10", params)

    brief_id = new_id()
    if out_path is None:
        out_dir = cfg.data_dir / "analysis"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"brief_{brief_id}.json"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md_path = out_path.with_suffix(".md")

    packet = {
        "brief_version": BRIEF_VERSION,
        "brief_id": brief_id,
        "person_id": person_id,
        "person_name": p["display_name"],
        "department": p["department"],
        "generated_at": clock.now_ms(),
        "period": period,
        "window": {"ts_start": ts_start, "ts_end": ts_end},
        "activity": {
            "frames": row["frames"] or 0,
            "work_minutes": round((row["work_f"] or 0) * interval / 60.0, 1),
            "idle_minutes": round((row["idle_f"] or 0) * interval / 60.0, 1),
            "days": row["days"] or 0,
            "apps": row["apps"] or 0,
            "top_apps": [
                {"app": a["app_name"],
                 "minutes": round(a["frames"] * interval / 60.0, 1)}
                for a in apps
            ],
            "note": "activity is scoped to the selected period; the report "
                    "below is the cumulative opportunity ledger",
        },
        "report": report,
        "output_md_path": str(md_path),
        "commit_command": f"digest brief commit --packet {out_path}",
        "instructions": INSTRUCTIONS,
    }
    out_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")

    return {
        "brief_id": brief_id,
        "packet_path": str(out_path),
        "output_md_path": str(md_path),
        "period": period,
        "opportunity_count": n_opps,
    }


def commit_brief(
    cfg: Config, store: Store, packet_path: str | Path,
    md_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Save the Claude-written brief Markdown as a digest row. Validates the
    packet round-trip so a half-finished brief never lands in the table."""
    packet_path = Path(packet_path)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    if packet.get("brief_version") != BRIEF_VERSION:
        raise ValueError(f"not a brief packet (or wrong version): {packet_path}")
    md_path = Path(md_path or packet["output_md_path"])
    if not md_path.exists():
        raise ValueError(
            f"brief Markdown not found at {md_path} — write the narrative there "
            "first (see the packet's instructions), then re-run commit.")
    content = md_path.read_text(encoding="utf-8").strip()
    if len(content) < 200:
        raise ValueError(
            f"brief at {md_path} is only {len(content)} chars — that is not a "
            "written brief. Write the full narrative, then re-run commit.")
    digest_id = store.insert_digest(
        person_id=packet["person_id"],
        period=f"exec-{packet.get('period') or '?'}",
        generated_at=clock.now_ms(),
        content_md=content,
        token_cost=0,   # spent in Claude Code, not metered here
    )
    return {"digest_id": digest_id, "person_id": packet["person_id"],
            "period": packet.get("period"), "bytes": len(content)}
