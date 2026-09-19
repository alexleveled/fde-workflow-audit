"""Phase 5 (beta) — the FastAPI reporting dashboard.

A local, read-mostly web UI over the same SQLite store the CLI uses. It presents
capture + analysis analytics for a chosen time window, shows each analysis run's
data summary and the current improvement suggestions, and can trigger the
model-free parts of the pipeline (rollup, analyze prepare, digest generate) as
background jobs. No Postgres — this beta stays on SQLite.

`create_app(cfg)` returns the FastAPI application; `run(cfg, host, port)` serves
it with uvicorn. Both import lazily so the base capture install needs neither
FastAPI nor uvicorn.
"""

from __future__ import annotations

__all__ = ["create_app", "run"]


def create_app(cfg):
    from .app import create_app as _create_app

    return _create_app(cfg)


def run(cfg, host: str = "127.0.0.1", port: int = 8787) -> None:
    from .app import run as _run

    _run(cfg, host=host, port=port)
