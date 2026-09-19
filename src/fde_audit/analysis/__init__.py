"""Analysis pass (Phase 2).

Model-free stages (sessionize, mining, prepare, commit) live here as pure Python
over `frames` metadata Phase 1 captured — re-runnable decisions, never a capture
migration. The only model step is the vision read, which Claude Code performs on
the representative frames a prepared packet points at.

Flow:
    digest analyze prepare  -> analysis_run (running) + packet.json (sessions,
                               mining signals, frames to look at)
    [Claude reads the sampled screenshots and fills results.json]
    digest analyze commit   -> validate labels against the taxonomy, write
                               summaries + observations, mark the run done — all
                               in one transaction.
"""
