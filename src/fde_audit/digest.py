"""Phase 4 — the digest generator.

Turns the Phase 3 `reporting.build_report()` dict into a **cadence report**: a
self-contained Markdown document that answers the video's core question — *"how
can I economize my workflow?"* — and, optionally, persists it to the `digests`
table so a scheduled/overnight run leaves a durable, shippable artifact.

Design notes, in line with the rest of the system:
  * **Model-free.** Phase 2 (vision) is the only place model spend happens. The
    digest is pure formatting over what the analysis pass already priced, so it
    is key-less and safe to run unattended (`token_cost = 0`). A later org run
    could swap in a model to write narrative prose; the persisted shape is ready
    for it (the `digests.token_cost` column exists).
  * **Weekly rates.** `est_minutes_per_week` is already normalized to a weekly
    figure by `commit._weekly`, so the digest reports per-week time cost
    regardless of the recording window. `period` is the cadence *label* (weekly
    / monthly), not a multiplier — it never fabricates time the data didn't show.
  * **Dollarized (optional).** If `[digest] loaded_hourly_rate` is set, weekly
    hours become a defensible `hours x rate` weekly cost — the "minutes x people
    x loaded rate" figure the plan calls for.

`generate_digest` returns a dict (with `content_md`); `render_markdown` does the
formatting; `save_digest` writes the `digests` row. No model calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .config import Config
from .db.store import Store
from .reporting import DEFAULT_STATUSES, build_report

# opportunity_type -> human label for Markdown (title-case, unlike the terminal
# report's all-caps).
_OPP_LABEL = {
    "automate": "Automate",
    "integrate": "Integrate",
    "consolidate": "Consolidate",
    "build-custom": "Build custom",
    "train": "Train",
    "eliminate": "Eliminate",
}


def _fmt_date(ts_ms: Optional[int]) -> str:
    if not ts_ms:
        return "—"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _opp_label(opp: Optional[str]) -> str:
    return _OPP_LABEL.get(opp or "", opp or "?")


def _dollars(hours: Optional[float], rate: Optional[float]) -> Optional[int]:
    """Weekly hours x loaded hourly rate -> whole-dollar weekly cost, or None."""
    if not hours or not rate:
        return None
    return round(hours * rate)


def _fmt_wk_cost(mins: Optional[float], rate: Optional[float]) -> str:
    """Compact per-week cost cell: '2.3h/wk' (+ '· $207/wk' if a rate is set)."""
    if not mins:
        return "—"
    hours = mins / 60.0
    time_s = f"{hours:.1f}h/wk" if mins >= 60 else f"{mins:.0f}m/wk"
    d = _dollars(hours, rate)
    return f"{time_s} · ${d:,}/wk" if d is not None else time_s


def _focus(report: dict[str, Any], top_n: int) -> list[dict[str, Any]]:
    """The recommended-action shortlist: the highest-cost *open/confirmed*
    opportunities (skip actioned/dismissed — already triaged) that carry a
    suggested upgrade. Opportunities are already ranked by weekly cost."""
    picks = [
        o for o in report["opportunities"]
        if o["status"] in ("open", "confirmed") and o.get("suggested_upgrade")
    ]
    if not picks:  # fall back to any open/confirmed row, priced or not
        picks = [o for o in report["opportunities"] if o["status"] in ("open", "confirmed")]
    return picks[:top_n]


# -- markdown rendering ---------------------------------------------------
def render_markdown(
    cfg: Config, report: dict[str, Any], *,
    period: str, person_name: Optional[str] = None, top_focus: int = 3,
) -> str:
    rate = cfg.digest.loaded_hourly_rate
    totals = report["totals"]
    scope = report["scope"]
    lines: list[str] = []

    # -- header
    subject = person_name or (f"department: {scope['department']}" if scope["department"]
                              else "all people")
    lines.append(f"# FDE Audit Digest — {period}")
    lines.append("")
    lines.append(f"*{subject} · generated {_fmt_date(report['generated_at'])}*")
    lines.append("")

    # -- headline
    n = totals["opportunity_count"]
    tot_h = totals["total_hours_per_week"]
    if n == 0:
        lines.append("No opportunities recorded yet. Record a work session and run "
                     "`digest analyze` to populate the ledger.")
        return "\n".join(lines) + "\n"
    tot_cost = _fmt_wk_cost(totals["total_minutes_per_week"], rate)
    lines.append(f"**{n} opportunit{'y' if n == 1 else 'ies'}** add up to **~{tot_cost}** "
                 "of reclaimable time.")
    lines.append("")

    # -- recommended focus (top of the doc: what to do first)
    focus = _focus(report, top_focus)
    if focus:
        lines.append(f"## Recommended focus this {period}")
        lines.append("")
        for i, o in enumerate(focus, start=1):
            cost = _fmt_wk_cost(o["est_minutes_per_week"], rate)
            head = o["description"] or o["pattern_key"]
            lines.append(f"{i}. **{_opp_label(o['opportunity_type'])}** — {head}  ")
            meta = f"~{cost}" if cost != "—" else "unpriced"
            lines.append(f"   _{meta}, {o['evidence_count'] or 0} sightings_")
            if o.get("suggested_upgrade"):
                lines.append(f"   → {o['suggested_upgrade']}")
        lines.append("")

    # -- full ranked table
    lines.append("## All opportunities")
    lines.append("")
    lines.append("| # | Type | Weekly cost | Status | Evidence | Opportunity |")
    lines.append("|--:|------|-------------|--------|---------:|-------------|")
    for o in report["opportunities"]:
        cost = _fmt_wk_cost(o["est_minutes_per_week"], rate)
        desc = (o["description"] or o["pattern_key"]).replace("|", "\\|")
        lines.append(
            f"| {o['rank']} | {_opp_label(o['opportunity_type'])} | {cost} | "
            f"{o['status']} | {o['evidence_count'] or 0} | {desc} |")
    lines.append("")

    # -- where the time goes
    bt = totals["by_opportunity_type"]
    if bt:
        lines.append("## Where the time goes")
        lines.append("")
        lines.append("**By opportunity type**")
        lines.append("")
        for k, v in bt.items():
            lines.append(f"- {_opp_label(k)} — {_fmt_wk_cost(v, rate)}")
        lines.append("")
    bd = totals["by_department"]
    if len(bd) > 1:
        lines.append("**By department**")
        lines.append("")
        for k, v in bd.items():
            lines.append(f"- {k} — {_fmt_wk_cost(v, rate)}")
        lines.append("")

    # -- consolidation (license-cut)
    con = report["consolidation"]
    if con:
        lines.append("## Consolidation candidates")
        lines.append("")
        lines.append("Same capability handled across multiple apps — overlap the org "
                     "can standardize (and cut licenses).")
        lines.append("")
        lines.append("| Capability | Apps | People | ~Hours/wk |")
        lines.append("|------------|------|-------:|----------:|")
        for c in con:
            apps = ", ".join(c["apps"]).replace("|", "\\|")
            lines.append(
                f"| {c['capability']} | {apps} | {c['people_count']} | "
                f"{c['est_hours_per_week']} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    weekly_note = "Time figures are per-week rates estimated from recorded frame spans."
    rate_note = (f" Dollar figures use a loaded rate of ${rate:,.0f}/h."
                 if rate else "")
    lines.append(f"*{weekly_note}{rate_note} Generated by `digest digest` (model-free).*")
    return "\n".join(lines) + "\n"


# -- generation + persistence --------------------------------------------
def generate_digest(
    cfg: Config, store: Store, *,
    period: Optional[str] = None,
    person_id: Optional[str] = None,
    department: Optional[str] = None,
    statuses: Optional[Iterable[str]] = DEFAULT_STATUSES,
    min_minutes: Optional[float] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """Build a digest dict (Markdown + metadata) over the current ledger. Does
    NOT persist — call `save_digest` for that. Model-free."""
    period = period or cfg.digest.default_period
    report = build_report(
        cfg, store, person_id=person_id, department=department,
        statuses=statuses, min_minutes=min_minutes, limit=limit,
    )
    person_name = None
    if person_id:
        p = store.get_person(person_id)
        person_name = p["display_name"] if p else None
    content_md = render_markdown(
        cfg, report, period=period, person_name=person_name,
        top_focus=cfg.digest.top_focus,
    )
    return {
        "period": period,
        "person_id": person_id,
        "person_name": person_name,
        "department": department,
        "generated_at": report["generated_at"],
        "token_cost": 0,   # deterministic, model-free
        "focus": _focus(report, cfg.digest.top_focus),
        "report": report,
        "content_md": content_md,
    }


def save_digest(store: Store, digest: dict[str, Any]) -> str:
    """Persist a digest to the `digests` table. Requires a single-person subject
    (the table + FK are person-scoped); org-wide (all-people / department) digests
    are view/file-only until the org rollout phase."""
    if not digest.get("person_id"):
        raise ValueError(
            "cannot save an org-wide digest — the digests table is person-scoped. "
            "Generate per-person (default) to persist, or write to a file with --out.")
    return store.insert_digest(
        person_id=digest["person_id"],
        period=digest["period"],
        generated_at=digest["generated_at"],
        content_md=digest["content_md"],
        token_cost=digest["token_cost"],
    )
