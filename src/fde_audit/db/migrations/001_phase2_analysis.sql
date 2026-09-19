-- Phase 2 (analysis pass) schema additions.
-- Frames metadata is untouched — sessionization and mining are re-runnable
-- analysis-time decisions over data Phase 1 already captured. These columns only
-- extend the derived, shippable tables the analysis pass writes.
--
-- Portability note: SQLite ALTER TABLE ADD COLUMN is a no-op-safe way to grow a
-- table; each runs once (guarded by schema_migrations). Postgres accepts the
-- same statements. New columns are all nullable / defaulted so existing rows
-- (and the Phase 1 code paths) stay valid.

-- summaries: controlled capability label + quantified signals from the pass.
ALTER TABLE summaries ADD COLUMN capability    TEXT;     -- controlled vocab (taxonomy.py) — powers consolidation GROUP BYs
ALTER TABLE summaries ADD COLUMN task          TEXT;     -- free text: the specific thing ("reconciling Chase account")
ALTER TABLE summaries ADD COLUMN input_density REAL;     -- 0..1, % of ticks with recent input (derived from idle_ms)
ALTER TABLE summaries ADD COLUMN friction      TEXT;     -- nullable; vision-flagged error dialogs / loading / repeated search
ALTER TABLE summaries ADD COLUMN boundary_reason TEXT;   -- why this session started (app-change | title-change | hash-jump | idle-gap | time-cap | first)

-- observations: turn each confirmed pattern into a typed, priced opportunity.
ALTER TABLE observations ADD COLUMN opportunity_type    TEXT;    -- automate | integrate | consolidate | build-custom | train | eliminate
ALTER TABLE observations ADD COLUMN est_minutes_per_week REAL;   -- computed from actual frame spans (ground-truth durations)
ALTER TABLE observations ADD COLUMN affected_people     INTEGER DEFAULT 1;  -- 1 solo; meaningful at org rollout
ALTER TABLE observations ADD COLUMN evidence_count      INTEGER DEFAULT 0;  -- distinct occurrences seen (the >=3-4 repeat confirmation)

-- Consolidation query support: capability totals per person/day.
CREATE INDEX IF NOT EXISTS idx_summaries_capability ON summaries(person_id, capability);
