# Usage reference

Full command reference for every phase of the pipeline. For the overview, install steps, and the demo, see the [README](../README.md).

## Capture

```
fde_audit db init                              # create SQLite schema (tables + indexes)
fde_audit person add --name "Jordan Lee" --dept developer
fde_audit capture start                        # run the recorder (Ctrl+C to stop)
fde_audit capture start --minutes 2            # bounded run (for verification)
fde_audit capture status                       # frames / dedupe / idle / disk stats
fde_audit rollup                               # build app_usage_rollup (free, no model)
fde_audit prune --days 14                      # delete screenshot files older than N days
```

Touch the pause file (`digest_data/PAUSE`) to suspend capture without stopping
the process. Denylisted apps/titles (password managers, banking - configurable
in `config.toml`) record metadata only, never a screenshot.

## Analysis pass (Phase 2)

The analysis pass turns captured frames into **typed, priced improvement
opportunities**. Everything except the vision read is model-free Python over the
`frames` metadata Phase 1 already stored - so sessionization and mining are
re-runnable decisions, never a capture-schema migration.

```
fde_audit analyze sessions            # multi-signal sessionization (debug, no run)
fde_audit analyze mine                # transition mining: ping-pong, rituals, bigrams (debug)
fde_audit analyze prepare [--all]     # open a run + write a packet.json for vision labeling
fde_audit analyze commit --packet P   # validate labels, write summaries + observations (1 txn)
fde_audit analyze status              # runs / summaries / capability mix / top opportunities
```

`prepare` (model-free) splits work frames into **window-sessions** - a new
session starts on any of: app change, window-title/URL change, a large
`frame_hash` jump (decomposes static-title apps like QuickBooks/Excel), an idle
gap, or an N-minute cap. It runs **transition mining** (A-B-A-B ping-pong loops =
manual data transfer; recurring daily app sequences = automation rituals; input
density = data-entry vs reading) to point the expensive vision reads at
hotspots, opens an `analysis_runs` row, and writes a packet listing the
representative screenshots to look at plus the controlled vocabularies.

### How Claude runs the analysis pass (the vision step)

The vision read is done by **Claude Code**, not an in-loop model. After
`fde_audit analyze prepare`:

1. Open the packet JSON. For each session, read every
   `representative_frames[*].path_abs` with vision.
2. Fill that session's `label`: `capability` (must be one of the packet's
   `capabilities` - the controlled vocab that makes app consolidation a
   `GROUP BY`), `task` (free text), `activity`, `category`, `low_value`,
   and `friction` (null or one of `friction_kinds`).
3. Optionally append typed entries to the top-level `opportunities` array
   (`opportunity_type` from `opportunity_types`, `pattern_key`, `description`,
   `suggested_upgrade`).
4. `fde_audit analyze commit --packet <that file>`.

The mining signals are only cheap **hints at hotspots** - they are not the list
of things to look for, and most opportunities come from the vision pass judging
what the screenshots actually show, across the full range of `opportunity_types`
(automate, integrate, consolidate, build-custom, train, eliminate), including
single-app inefficiency (an unused hotkey, manual formatting, slow navigation),
not just cross-app switching.

`commit` validates every label against the taxonomy, then in **one transaction**
writes one `summaries` row per session, upserts the `observations` ledger, and
marks the run `done`. Some observations are derived automatically (ping-pong →
`integrate`, rituals → `automate`, low-value spans → `eliminate`, friction →
mapped, and a `capability` spread across ≥2 apps → `consolidate` - the license-cut
signal, computed over all summaries); the rest are the typed `opportunities`
Claude adds from vision. Each is priced with `est_minutes_per_week` from real
frame spans where applicable and confirmed after `observation_confirm` repeats. An invalid label rolls the whole thing back and marks the run
`failed`, so a bad pass strands nothing - the "what's analyzed" watermark only
advances on a completed run. Re-running `prepare` analyzes only frames past the
watermark; `--all` re-analyzes everything.


## Reporting & confirmation (Phase 3)

The analysis pass fills an `observations` ledger with typed, priced opportunities.
Phase 3 is the read/act layer on top of it - no model calls, pure SQL:

```
fde_audit report                      ranked opportunities by weekly time cost
  --department finance             slice to one department (org view)
  --all-people                     every person, not just the first
  --type integrate                 filter to one opportunity_type
  --min-minutes 30                 hide small opportunities
  --all-status                     include dismissed rows
  --json                           machine-readable (Phase 4 digest consumes this)

fde_audit observe list                the ledger with short ids
fde_audit observe accept <ref>        mark actioned (a real opportunity you're pursuing)
fde_audit observe dismiss <ref>       mark dismissed (false positive / not relevant)
fde_audit observe reopen <ref>        clear the human status; recompute by evidence
```

`<ref>` is a short id (shown in the report), an id prefix, or the exact
`pattern_key`. The report ranks by `est_minutes_per_week`, rolls weekly time up by
opportunity type and by department, and shows **consolidation candidates** - any
`capability` handled across ≥2 apps, aggregated across people (the org license-cut
signal). `accept`/`dismiss` write a human status that the analysis pass never
overwrites, so re-running `fde_audit analyze` won't undo your triage.


## Digest generator (Phase 4)

The digest turns the Phase 3 report into a **cadence document** - a self-contained
Markdown artifact that answers *"how can I economize my workflow?"* and (for a
single person) persists to the `digests` table, so a scheduled/overnight run
leaves a durable, shippable record. Like the report it is **model-free**: pure
formatting over what the analysis pass already priced (`token_cost = 0`), so it is
key-less and safe to run unattended.

```
fde_audit digest generate             build a Markdown digest from the current ledger (saves it)
  --period monthly                 cadence label stamped on the digest (default from config)
  --department finance             org-wide slice (view/file only - not saved)
  --all-people                     every person (org view - not saved)
  --min-minutes 30                 hide small opportunities
  --out digest.md                  write the Markdown to a file
  --json                           the full digest dict (Markdown + the report it wraps)
  --no-save                        don't persist to the digests table

fde_audit digest list                 saved digests (short id, day, period, person)
fde_audit digest show <id>            print a saved digest's Markdown
```

The document leads with a **Recommended focus** shortlist (the highest-cost
open/confirmed opportunities with a concrete upgrade), then a full ranked table,
weekly-time rollups by opportunity type and department, and the cross-app
**consolidation** (license-cut) candidates. Time figures are the per-week rates
the analysis pass computed; `period` is a cadence *label*, never a multiplier. Set
`[digest] loaded_hourly_rate` in `config.toml` to dollarize weekly hours (the
`minutes x people x loaded rate` figure). Persisting is **person-scoped** (the
`digests` table + FK carry `person_id`); org-wide digests are view/file-only until
the Postgres rollout.

**What the digest is (and isn't).** The digest is a *data report*: a
deterministic ranked table + rollups over the opportunities the analysis pass
already wrote. The AI's improvement-finding happened earlier, at
`analyze commit` time - the digest never invents an insight, it only ranks,
totals and formats the stored ones. It does not write narrative prose ("build
this custom app", "cut these licenses") - that is the **executive brief** below.

## Executive brief (narrative report, written by Claude Code)

The brief is the report a manager reads: 2-4 themes behind the opportunities,
prioritized recommendations (custom apps/scripts worth building, licenses to cut
via the consolidation candidates, integrations to set up), what's *not* worth
acting on, and next steps. Writing that takes model judgment, so - like the
vision pass, and still with **no API key** - it round-trips through Claude Code:

```
fde_audit brief prepare [--period 7d]   bundle ledger + period activity into a packet (model-free)
   -> Claude Code reads the packet, writes the narrative Markdown to the
      packet's output_md_path ("Write the executive brief from the prepared brief packet.")
fde_audit brief commit --packet P       validate + save it to the digests table (period `exec-<period>`)
```

`--period` (1d/7d/30d/90d/all) scopes the *activity stats* in the packet; the
opportunity ledger is always the full cumulative evidence. The saved brief shows
up alongside data reports in `fde_audit digest list` and the dashboard's
"Saved reports", tagged `exec-7d` etc. `commit` refuses a missing or trivially
short Markdown file, so a half-finished brief never lands in the table.

## Dashboard (Phase 5, beta)

A local web UI over the same SQLite store - capture + analysis analytics for a
chosen person and time window, the improvement report, per-run data summaries,
saved digests, and buttons to trigger the model-free pipeline steps. **SQLite
only** (no Postgres in the beta); FastAPI + uvicorn are an optional extra so the
base capture install stays dependency-light.

```
pip install -e .[dashboard]        # one-time: adds fastapi + uvicorn
fde_audit dashboard                   # serve at http://127.0.0.1:8787
fde_audit dashboard --host 0.0.0.0 --port 9000
```

The page (one self-contained file, no external assets, light/dark aware) shows:

- an **overview** tile row for the selected window (24h / 7d / 30d / 90d / all) -
  work time, images stored + how many were de-duplicated, frames captured, idle
  time, sessions analyzed, estimated reclaimable minutes/week, open opportunities;
- **time by app** and **daily activity** charts;
- the ranked **improvement suggestions** (the Phase 3 report) + consolidation
  candidates;
- **analysis runs** - click a run to see the labeled `summaries` it produced (the
  data its suggestions came from);
- **saved reports (digests)** - click to read the rendered Markdown.

The "Run an analysis" panel lays the pipeline out as a numbered stepper (with a
built-in "how do I…" accordion listing every step and the exact phrase to tell
Claude Code). Buttons run as background jobs; the two model steps are shown as
chips because they happen in Claude Code, not in the dashboard:

1. **Prepare vision analysis** - `fde_audit analyze prepare` (sessionize + mine +
   open a run + write the packet). Shows an "N new" badge fed by the
   "Awaiting analysis" tile.
2. *(chip)* **Tell Claude Code to label the packet** - the vision step, where all
   the improvement-finding intelligence enters; ends with
   `fde_audit analyze commit --packet …`.
3. **Compile report + brief packet** - one click, two model-free processes back
   to back: `fde_audit digest generate` + save (the ranked data table over the
   already-AI-written ledger - receipts, not prose) **and** `fde_audit brief prepare`
   (bundles the ledger + the Period-selector activity into a brief packet). The
   job log shows a "view data report" link and what to tell Claude next.
4. *(chip)* **Tell Claude Code to write the brief** - Claude writes the narrative
   Markdown and runs `fde_audit brief commit --packet …`; the brief lands under
   "Saved reports" with an `exec-…` period tag.

Steps 3 and 4 were originally separate "compile report" and "prepare brief"
buttons; since both are instant, model-free processing they're merged into one
click, leaving the flow a clean alternation of machine step (1, 3) and Claude
Code step (2, 4). The CLI keeps them separate (`fde_audit digest generate`,
`fde_audit brief prepare`) for scripting.

The `app_usage_rollup` pre-aggregation (`fde_audit rollup`, CLI-only) is **not**
exposed as a dashboard button in the beta: nothing single-user reads it - "Time by
app" is computed live from `frames` on every load - so a button would only mislead.
The rollup is a *shippable* table for the org rollout, where each machine syncs its
small daily rollup to central Postgres; it earns a UI there, not here.

## What each tick does

Two independent streams, never conflated:

- **Metadata row** (`frames`) - written every tick for active/passive work time;
  the sole source of time-in-app. Idle is collapsed (onset + ≤1 heartbeat/min).
- **Image** - the only thing dedupe trims. A near-duplicate screen writes a
  `dup_of` row with **no new image file**.

Three activity states (`active` / `passive` / `idle`) are derived from two free
signals: input idle time (`GetLastInputInfo`) and pixel change (`frame_hash`, a
pure-Python average-hash of a downsampled frame). Reading without input stays
`passive` up to `passive_grace_seconds` so real reading time is never dropped.

## Layout

```
config.toml                 capture interval, dirs, retention, image format, privacy
src/fde_audit/
  config.py                 tomllib load/validate, path resolution
  clock.py                  UTC epoch-millis, local_day, tz offset
  platform/windows.py       ctypes: DPI awareness, foreground window, idle, monitor, mutex
  capture/watcher.py        the loop (state machine, dedupe, idle collapse)
  capture/screenshot.py     mss grab + average-hash + JPEG/PNG output
  capture/privacy.py        denylist + pause file
  db/schema.sql, store.py   portable schema (SQLite now, Postgres later) + data access
  db/migrations/            numbered .sql (001_phase2_analysis.sql adds the Phase 2 columns)
  analysis/                 Phase 2 analysis pass (model-free except the vision read):
    taxonomy.py               controlled capability + opportunity-type vocabularies
    sessionize.py             multi-signal window-session splitting
    mining.py                 ping-pong / bigrams / rituals / input density
    prepare.py                write the vision packet + open an analysis_run
    commit.py                 validate labels, write summaries + observations (1 txn)
  reporting.py              Phase 3: ranked opportunity report + consolidation view
  digest.py                 Phase 4: model-free Markdown cadence digest over the ledger
  brief.py                  executive brief: packet prepare + commit (narrative written by Claude Code)
  dashboard/                Phase 5 (beta): FastAPI reporting dashboard over SQLite
    service.py                period-scoped read queries (capture + analysis analytics)
    jobs.py                   background-job runner for the model-free triggers
    app.py                    FastAPI app + JSON API
    static/index.html         one self-contained, theme-aware page (no CDN)
  rollup.py                 free text-tier app_usage_rollup
  retention.py              prune old screenshots, keep metadata
  cli.py                    the `fde_audit` command
run_capture.py              convenience entrypoint
tests/                      unittest suite + synthetic scenario (tests/synth.py)
```
