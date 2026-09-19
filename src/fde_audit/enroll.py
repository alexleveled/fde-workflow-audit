"""Fleet auto-enrollment: derive a person's identity and department from the OS
at capture time, so pushing the agent to a whole department needs no manual
`person add` per employee.

Two resolutions, both best-effort with safe fallbacks:

* Identity (the stable natural key stored as people.ext_key): the Windows account
  SID — assigned by Active Directory at account creation, unchanged across
  reinstalls, re-pushes, and username changes. Falls back to DOMAIN\\user, then
  host:<hostname>, so enrollment never hard-fails.

* Department: the AD `department` attribute for that user, read live from the
  domain controller. When the box is off-domain or the attribute is empty, it
  silently falls back to the package's config default — i.e. the value you baked
  into the per-department config.toml before handing the package to IT.

Precedence, exactly as requested: Active Directory first, package config second.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import clock
from .config import Config
from .db.store import Store
from .platform import os_name

log = logging.getLogger(__name__)


def _try(fn, *args):
    """Call a best-effort platform hook, swallowing any failure to None."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 — identity probes must never crash capture
        return None


def resolve_identity(plat) -> tuple[str, str]:
    """Return (ext_key, display_name).

    ext_key is the stable key used to find-or-create the person; display_name is
    the human-facing label shown in the dashboard.
    """
    sid = _try(getattr(plat, "user_sid", lambda: None))
    login = _try(getattr(plat, "user_login", lambda: None))
    host = plat.hostname()
    display = login or host
    ext_key = sid or login or f"host:{host}"
    return ext_key, display


def resolve_department(cfg: Config, plat, sid: Optional[str]) -> tuple[str, str]:
    """Return (department, source). AD attribute first; silently fall back to the
    packaged config default when AD is unavailable or the attribute is empty."""
    dept = _try(getattr(plat, "ad_department", lambda _s=None: None), sid)
    if dept:
        return dept, "active-directory"
    return cfg.identity.default_department, "config-default"


def auto_enroll(cfg: Config, store: Store, plat):
    """Find-or-create the person for the current OS user and register this
    device. Idempotent: the same user on the same machine re-runs to the same
    person + device rows. Returns (person_row, device_id)."""
    ext_key, display = resolve_identity(plat)
    sid = ext_key if ext_key.startswith("S-") else None
    dept, source = resolve_department(cfg, plat, sid)

    person = store.get_person_by_ext_key(ext_key)
    if person is None:
        pid = store.add_person(
            display_name=display,
            department=dept,
            created_at=clock.now_ms(),
            ext_key=ext_key,
        )
        person = store.get_person(pid)
        log.info(
            "auto-enrolled %s (%s) department=%s via %s [key=%s]",
            display, pid, dept, source, ext_key,
        )
    elif source == "active-directory" and person["department"] != dept:
        # AD is authoritative when it answers — self-correct a stale label
        # (e.g. the employee transferred departments).
        store.set_person_department(person["person_id"], dept)
        log.info("updated %s department %s -> %s (AD)",
                 display, person["department"], dept)
        person = store.get_person(person["person_id"])

    did = store.upsert_device(
        person_id=person["person_id"],
        hostname=plat.hostname(),
        os_name=os_name(),
        tz=plat.tz_name(),
        created_at=clock.now_ms(),
    )
    return person, did
