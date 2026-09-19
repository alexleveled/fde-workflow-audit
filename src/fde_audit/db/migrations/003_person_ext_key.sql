-- People get a stable natural key (ext_key) so a fleet push can auto-enroll the
-- current OS user without a manual `person add`. The key is the Windows account
-- SID (survives reinstalls / username changes), falling back to DOMAIN\user or
-- host:<hostname>. Nullable: existing UUID-seeded people keep working untouched.
ALTER TABLE people ADD COLUMN ext_key TEXT;

-- One person per natural key. Partial index so the many legacy NULLs don't
-- collide (SQLite treats NULLs as distinct anyway, but this is explicit and
-- portable to the Postgres rollout).
CREATE UNIQUE INDEX IF NOT EXISTS idx_people_ext_key
    ON people(ext_key) WHERE ext_key IS NOT NULL;
