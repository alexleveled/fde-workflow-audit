"""Phase 3 — the observations report.

Read-only over the ledger that `commit.py` already writes. Turns typed/priced
`observations` rows into a ranked, sliceable view:

  * **opportunities** — ordered by estimated weekly time cost (the defensible
    `minutes x people x rate` input), each carrying its opportunity type, status,
    evidence count, and suggested upgrade;
  * **totals** — weekly minutes rolled up by opportunity type and by department
    (the org slice);
  * **consolidation** — a cross-person view of any `capability` spread across
    multiple apps (the license-cut signal), generalized from the solo
    `capability_app_spread` to a GROUP BY over the whole `people` table.

`build_report` returns a plain dict (JSON-ready, and the input the Phase 4 digest
generator will consume); `render_text` formats it for the terminal. No model
calls — this is all SQL over what the analysis pass produced.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from . import clock
from .config import Config
from .db.store import Store

# statuses shown by default: everything except a human "dismissed" (false
# positive). `--all` re-includes dismissed.
DEFAULT_STATUSES: tuple[str, ...] = ("open", "confirmed", "actioned")


def _hours(minutes: Optional[float]) -> Optional[float]:
    return round(minutes / 60.0, 1) if minutes else None


def build_report(
    cfg: Config, store: Store, *,
    person_id: Optional[str] = None,
    department: Optional[str] = None,
    statuses: Optional[Iterable[str]] = DEFAULT_STATUSES,
    opportunity_types: Optional[Iterable[str]] = None,
    min_minutes: Optional[float] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """Assemble the report as a plain dict. `statuses=None` includes every
    status (used by `--all`)."""
    rows = store.list_observations(
        person_id=person_id, department=department, statuses=statuses,
        opportunity_types=opportunity_types, min_minutes=min_minutes, limit=limit,
    )

    opportunities: list[dict[str, Any]] = []
    by_type: dict[str, float] = {}
    by_dept: dict[str, float] = {}
    total_minutes = 0.0
    for i, r in enumerate(rows, start=1):
        mins = r["est_minutes_per_week"]
        opp = r["opportunity_type"] or "?"
        dept = r["department"]
        if mins:
            total_minutes += mins
            by_type[opp] = by_type.get(opp, 0.0) + mins
            by_dept[dept] = by_dept.get(dept, 0.0) + mins
        opportunities.append({
            "rank": i,
            "id": r["id"],
            "short_id": r["id"][:8],
            "pattern_key": r["pattern_key"],
            "opportunity_type": r["opportunity_type"],
            "status": r["status"],
            "person": r["display_name"],
            "department": dept,
            "est_minutes_per_week": mins,
            "est_hours_per_week": _hours(mins),
            "evidence_count": r["evidence_count"],
            "occurrences": r["occurrences"],
            "affected_people": r["affected_people"],
            "description": r["description"],
            "suggested_upgrade": r["suggested_upgrade"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
        })

    return {
        "generated_at": clock.now_ms(),
        "scope": {
            "person_id": person_id,
            "department": department,
            "statuses": list(statuses) if statuses is not None else "all",
        },
        "opportunities": opportunities,
        "totals": {
            "opportunity_count": len(opportunities),
            "total_minutes_per_week": round(total_minutes, 1),
            "total_hours_per_week": _hours(total_minutes),
            "by_opportunity_type": {k: round(v, 1) for k, v in
                                    sorted(by_type.items(), key=lambda kv: -kv[1])},
            "by_department": {k: round(v, 1) for k, v in
                              sorted(by_dept.items(), key=lambda kv: -kv[1])},
        },
        "consolidation": _consolidation(cfg, store, department),
    }


def _consolidation(
    cfg: Config, store: Store, department: Optional[str]
) -> list[dict[str, Any]]:
    """Cross-person: a capability handled across >= 2 apps is a consolidation
    candidate. Aggregates apps and people so the org sees 'reconciliation runs in
    3 apps across 4 people' — the anecdote a per-person observation can't show."""
    interval = cfg.capture.interval_seconds
    spread: dict[str, dict[str, Any]] = {}
    for r in store.capability_app_spread_all(department):
        cap = r["capability"]
        g = spread.setdefault(cap, {
            "apps": {}, "people": set(), "departments": set(),
            "frames": 0, "first": r["first_seen"], "last": r["last_seen"],
        })
        g["apps"][r["app_name"]] = g["apps"].get(r["app_name"], 0) + (r["frames"] or 0)
        g["people"].add(r["person_id"])
        g["departments"].add(r["department"])
        g["frames"] += r["frames"] or 0
        g["first"] = min(g["first"], r["first_seen"])
        g["last"] = max(g["last"], r["last_seen"])

    out: list[dict[str, Any]] = []
    for cap, g in spread.items():
        apps = sorted(g["apps"], key=lambda a: g["apps"][a], reverse=True)
        if len(apps) < 2:
            continue
        out.append({
            "capability": cap,
            "apps": apps,
            "app_count": len(apps),
            "people_count": len(g["people"]),
            "departments": sorted(g["departments"]),
            "est_hours_per_week": round(g["frames"] * interval / 3600.0, 1),
            "first_seen": g["first"],
            "last_seen": g["last"],
        })
    out.sort(key=lambda c: (c["app_count"], c["est_hours_per_week"]), reverse=True)
    return out


# -- text rendering -------------------------------------------------------
_OPP_LABEL = {
    "automate": "AUTOMATE", "integrate": "INTEGRATE", "consolidate": "CONSOLIDATE",
    "build-custom": "BUILD", "train": "TRAIN", "eliminate": "ELIMINATE",
}


def _fmt_minutes(mins: Optional[float]) -> str:
    if not mins:
        return "     n/a"
    if mins >= 60:
        return f"{mins / 60:6.1f}h"
    return f"{mins:6.0f}m"


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    scope = report["scope"]
    totals = report["totals"]
    where = []
    if scope["person_id"]:
        where.append(f"person={scope['person_id'][:8]}")
    if scope["department"]:
        where.append(f"dept={scope['department']}")
    scope_str = ("  [" + ", ".join(where) + "]") if where else ""
    lines.append("=" * 72)
    lines.append(f"FDE AUDIT - opportunity report{scope_str}")
    tot_h = totals["total_hours_per_week"]
    tot = f"{tot_h}h/wk" if tot_h else f"{totals['total_minutes_per_week']}m/wk"
    lines.append(f"{totals['opportunity_count']} opportunities  |  "
                 f"~{tot} of estimated weekly time cost")
    lines.append("=" * 72)

    if not report["opportunities"]:
        lines.append("")
        lines.append("No opportunities yet — run `digest analyze` on a recorded session.")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"{'#':>2}  {'TYPE':<11} {'WK COST':>8}  {'STATUS':<10} "
                 f"{'EVID':>4}  {'ID':<8}  OPPORTUNITY")
    lines.append("-" * 72)
    for o in report["opportunities"]:
        typ = _OPP_LABEL.get(o["opportunity_type"], o["opportunity_type"] or "?")
        lines.append(
            f"{o['rank']:>2}  {typ:<11} {_fmt_minutes(o['est_minutes_per_week'])}  "
            f"{o['status']:<10} {o['evidence_count'] or 0:>4}  "
            f"{o['short_id']:<8}  {o['description'] or o['pattern_key']}")
        if o["suggested_upgrade"]:
            lines.append(f"{'':>41}-> {o['suggested_upgrade']}")

    # rollups
    bt = totals["by_opportunity_type"]
    if bt:
        lines.append("")
        lines.append("Weekly time by opportunity type:")
        for k, v in bt.items():
            lines.append(f"  {_OPP_LABEL.get(k, k):<12} {_fmt_minutes(v)}")
    bd = totals["by_department"]
    if len(bd) > 1:
        lines.append("")
        lines.append("Weekly time by department:")
        for k, v in bd.items():
            lines.append(f"  {k:<14} {_fmt_minutes(v)}")

    con = report["consolidation"]
    if con:
        lines.append("")
        lines.append("Consolidation candidates (same capability, multiple apps):")
        for c in con:
            ppl = f", {c['people_count']} people" if c["people_count"] > 1 else ""
            lines.append(
                f"  {c['capability']:<16} {c['app_count']} apps{ppl}, "
                f"~{c['est_hours_per_week']}h/wk: {', '.join(c['apps'])}")

    return "\n".join(lines)
