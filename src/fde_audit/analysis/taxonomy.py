"""Controlled vocabularies for the analysis pass.

Free text is unqueryable across people and departments. A fixed `capability`
vocabulary is what turns app consolidation into a GROUP BY — "3 departments spend
40 hrs/wk on task-tracking split across Asana, Trello, Monday" is only answerable
if every summary tags its function from the same closed set. `task` stays free
text for the specific thing; `capability` is the closed set below.

Kept deliberately small (~20). Add values here (a code change, reviewed) rather
than letting the vision pass invent labels — an open set silently fragments the
very rollups it exists to feed.
"""

from __future__ import annotations

# ~20 functional capabilities. The point is cross-app comparability, so values
# name the *function* (what work is being done), never the app.
CAPABILITIES: tuple[str, ...] = (
    "task-tracking",       # boards, tickets, to-dos (Asana/Jira/Trello/Monday)
    "invoicing",           # creating/sending bills
    "reconciliation",      # matching transactions/accounts (QuickBooks/Excel)
    "bookkeeping",         # general ledger / accounting entry
    "comms-internal",      # Slack/Teams/internal chat
    "comms-external",      # email/customer messaging
    "meetings",            # video calls, scheduling-in-progress
    "reporting",           # building reports/dashboards/decks
    "data-entry",          # typing structured data into a system
    "data-analysis",       # reading/analyzing data, spreadsheets, BI
    "scheduling",          # calendars, booking
    "file-mgmt",           # moving/renaming/organizing files, uploads/downloads
    "research",            # web/doc reading to find information
    "coding",              # writing/editing source, IDE work
    "code-review",         # reviewing diffs/PRs
    "design",              # visual/UX design tools
    "documentation",       # writing docs/notes/wikis
    "crm",                 # customer records, pipeline, sales tooling
    "hr-admin",            # HR, payroll, onboarding admin
    "support",             # handling support tickets/customer issues
    "browsing-idle",       # low-intent browsing, context with no clear task
    "other",               # genuinely doesn't fit — a signal to grow this list
)

# Typed opportunities. Each confirmed observation is one of these, not an anecdote.
OPPORTUNITY_TYPES: tuple[str, ...] = (
    "automate",       # a repeated manual sequence a script/macro could do
    "integrate",      # two systems manually kept in sync -> connect them
    "consolidate",    # same capability across N apps -> cut to one
    "build-custom",   # a bespoke small app/extension would collapse the work
    "train",          # the tool can already do it faster (hotkey/feature unused)
    "eliminate",      # the activity is low-value; stop doing it
)

# Friction is free-ish but we suggest a small controlled set for aggregation.
FRICTION_KINDS: tuple[str, ...] = (
    "error-dialog",
    "loading-wait",
    "repeated-search",
    "manual-copy",
    "context-switch",
    "rework",
)

_CAP_SET = frozenset(CAPABILITIES)
_OPP_SET = frozenset(OPPORTUNITY_TYPES)


def is_capability(value: str | None) -> bool:
    return value in _CAP_SET


def is_opportunity_type(value: str | None) -> bool:
    return value in _OPP_SET


def validate_capability(value: str | None) -> str:
    """Normalize + validate a capability label. Raises ValueError on an unknown
    value so a mislabeled commit fails loudly instead of fragmenting rollups."""
    v = (value or "").strip().lower()
    if v not in _CAP_SET:
        raise ValueError(
            f"unknown capability {value!r}; must be one of {', '.join(CAPABILITIES)}"
        )
    return v


def validate_opportunity_type(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v not in _OPP_SET:
        raise ValueError(
            f"unknown opportunity_type {value!r}; must be one of {', '.join(OPPORTUNITY_TYPES)}"
        )
    return v
