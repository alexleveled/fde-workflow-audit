"""Retention: delete screenshot files older than N days, null screenshot_path,
set pruned=1. Frame metadata, summaries, and rollups persist forever."""

from __future__ import annotations

import logging
from pathlib import Path

from . import clock
from .config import Config
from .db.store import Store

log = logging.getLogger("digest.retention")


def prune(cfg: Config, store: Store, days: int | None = None) -> dict:
    """Delete screenshot files older than `days` (default from config).
    Returns {"deleted": n, "bytes_freed": b, "missing": m}."""
    retain_days = cfg.retention.screenshot_days if days is None else days
    cutoff = clock.now_ms() - retain_days * 86_400_000
    rows = store.frames_to_prune(cutoff)

    deleted = bytes_freed = missing = 0
    for r in rows:
        rel = r["screenshot_path"]
        if rel:
            path = Path(rel)
            if not path.is_absolute():
                path = cfg.data_dir / path
            try:
                if path.exists():
                    bytes_freed += path.stat().st_size
                    path.unlink()
                    deleted += 1
                else:
                    missing += 1
            except OSError:
                log.exception("failed to delete %s", path)
        store.mark_pruned(r["id"])
    store.commit()
    return {"deleted": deleted, "bytes_freed": bytes_freed, "missing": missing}
