"""Seed a self-contained demo workspace so the dashboard can be explored without
recording your own screen.

Builds `demo/` next to the repo root with its own config.toml and SQLite store,
fills it with five synthetic workdays for a fictional finance analyst, runs the
real analysis pipeline over them (prepare -> label -> commit), and saves a
digest. The only step that is faked is the vision read: labels come from a fixed
table below instead of a model looking at screenshots.

    python scripts/seed_demo.py
    fde_audit --config demo/config.toml dashboard

Deterministic (seeded RNG), and safe to re-run: it wipes `demo/` first.
"""

from __future__ import annotations

import json
import random
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fde_audit.analysis import commit as commit_mod  # noqa: E402
from fde_audit.analysis.prepare import prepare  # noqa: E402
from fde_audit.config import load_config  # noqa: E402
from fde_audit.db.store import Store, new_id  # noqa: E402
from fde_audit.digest import generate_digest, save_digest  # noqa: E402

DEMO = ROOT / "demo"
INTERVAL_MS = 5_000
MIN_MS = 60_000

# (app, title, url_domain, minutes, state, capability, low_value, friction)
# A block is a stretch of continuous work in one window. "pingpong" is expanded
# into short alternating Excel/QuickBooks stints (manual data transfer).
DAY_PLAN = [
    ("Outlook", "Inbox", None, 25, "active", "comms-external", False, None),
    ("Chrome", "Bank portal: download statements", "bank.example.com", 12, "active", "reconciliation", False, "loading-wait"),
    ("Excel", "bank-import.xlsx", None, 18, "active", "reconciliation", False, "manual-copy"),
    ("pingpong", None, None, 30, "active", "reconciliation", False, "manual-copy"),
    ("Teams", "Daily standup", None, 20, "active", "meetings", False, None),
    ("Slack", "#finance-ops", None, 12, "active", "comms-internal", False, None),
    ("Asana", "Month-end close checklist", "app.asana.com", 8, "active", "task-tracking", False, None),
    ("Trello", "AP follow-ups", "trello.com", 8, "active", "task-tracking", False, "context-switch"),
    ("ERP", "Vendor invoice entry", None, 40, "active", "data-entry", False, "rework"),
    ("Chrome", "Industry news", "news.example.com", 14, "active", "browsing-idle", True, None),
    ("Excel", "month-end-report.xlsx", None, 45, "active", "reporting", False, None),
    ("PowerPoint", "Monthly finance review.pptx", None, 30, "active", "reporting", False, None),
    ("Chrome", "Revenue recognition guidance", "docs.example.com", 20, "passive", "research", False, None),
    ("Outlook", "Inbox", None, 15, "active", "comms-external", False, "repeated-search"),
]

# Typed opportunities of the kind the vision pass adds from what the screenshots
# show (the pipeline derives integrate/automate/eliminate/consolidate itself).
OPPORTUNITIES = [
    {
        "opportunity_type": "build-custom",
        "pattern_key": "erp-invoice-rekeying",
        "description": "Vendor invoices are read from PDF and re-keyed field by field into the ERP.",
        "suggested_upgrade": "A small extraction script that reads invoice PDFs and produces the ERP's bulk-import CSV.",
        "est_minutes_per_week": 150,
        "occurrences": 5,
        "category": "data-entry",
    },
    {
        "opportunity_type": "automate",
        "pattern_key": "bank-statement-download",
        "description": "Every morning starts with logging into the bank portal, downloading statements, and pasting them into a workbook.",
        "suggested_upgrade": "Pull the bank feed on a schedule (bank API or aggregator) straight into the reconciliation workbook.",
        "est_minutes_per_week": 75,
        "occurrences": 5,
        "category": "reconciliation",
    },
    {
        "opportunity_type": "train",
        "pattern_key": "manual-report-formatting",
        "description": "Month-end report columns and number formats are fixed by hand each time.",
        "suggested_upgrade": "Save the report as an Excel template with table styles so formatting is applied on paste.",
        "est_minutes_per_week": 35,
        "occurrences": 4,
        "category": "reporting",
    },
]


def _hash(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(64))


def _expand(plan, rng):
    """Yield (app, title, domain, ms, state, cap, low, friction) blocks with jitter."""
    for app, title, domain, minutes, state, cap, low, fr in plan:
        minutes = max(3, round(minutes * rng.uniform(0.75, 1.25)))
        if app == "pingpong":
            left = minutes * MIN_MS
            side = 0
            while left > 0:
                stint = min(left, rng.randint(40, 110) * 1000)
                a, t = (("Excel", "reconciliation.xlsx"), ("QuickBooks", "Bank reconciliation"))[side]
                yield (a, t, None, stint, state, cap, low, fr)
                left -= stint
                side ^= 1
        else:
            yield (app, title, domain, minutes * MIN_MS, state, cap, low, fr)


def _write_config() -> Path:
    src = (ROOT / "config.toml").read_text(encoding="utf-8")
    src = src.replace('data_dir = "./digest_data"', 'data_dir = "./data"')
    DEMO.mkdir(parents=True, exist_ok=True)
    path = DEMO / "config.toml"
    path.write_text(src, encoding="utf-8")
    return path


def main() -> None:
    if DEMO.exists():
        shutil.rmtree(DEMO)
    cfg = load_config(_write_config())
    cfg.screenshots_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    store.init_db()

    rng = random.Random(42)
    now_ms = int(time.time() * 1000)
    pid = store.add_person(display_name="Jordan Lee", department="finance", created_at=now_ms)
    did = store.upsert_device(pid, hostname="DEMO-LAPTOP", os_name="Windows", tz="UTC", created_at=now_ms)

    # Five most recent weekdays before today, 09:00 UTC start.
    days, d = [], datetime.now(timezone.utc).date()
    while len(days) < 5:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    days.reverse()

    label_by_block: dict[tuple[str, str], dict] = {}
    n_frames = 0
    for day in days:
        t = int(datetime(day.year, day.month, day.day, 9, tzinfo=timezone.utc).timestamp() * 1000)
        t += rng.randint(0, 20) * MIN_MS
        plan = DAY_PLAN[:]
        # Shuffle the middle of the day a little; mornings and wrap-up stay put.
        mid = plan[4:-2]
        rng.shuffle(mid)
        plan = plan[:4] + mid + plan[-2:]
        for app, title, domain, ms, state, cap, low, fr in _expand(plan, rng):
            label_by_block[(app, title)] = {"capability": cap, "low_value": low, "friction": fr}
            h = _hash(rng)
            first_id = None
            end = t + ms
            while t < end:
                fid = new_id()
                shot = None
                if first_id is None:
                    rel = f"{day.isoformat()}/{fid}.jpg"
                    (cfg.screenshots_dir / day.isoformat()).mkdir(parents=True, exist_ok=True)
                    (cfg.screenshots_dir / rel).write_bytes(b"\xff\xd8\xff\xe0demo")
                    shot = f"screenshots/{rel}"
                idle = rng.randint(30_000, 55_000) if state == "passive" else rng.randint(0, 4_000)
                store.insert_frame({
                    "id": fid, "person_id": pid, "device_id": did, "ts_utc": t,
                    "local_day": day.isoformat(), "tz_offset_min": 0, "app_name": app,
                    "exe_path": f"C:\\Program Files\\{app}\\{app}.exe", "window_title": title,
                    "url_domain": domain, "idle_ms": idle, "activity_state": state,
                    "is_heartbeat": 0, "screenshot_path": shot,
                    "dup_of": None if first_id is None else first_id, "frame_hash": h,
                    "pruned": 0, "screen_w": 1920, "screen_h": 1080, "created_at": t,
                })
                first_id = first_id or fid
                n_frames += 1
                t += INTERVAL_MS
            t += rng.randint(0, 3) * MIN_MS  # small gaps between blocks

    info = prepare(cfg, store, pid, all_frames=True)
    packet_path = Path(info["packet_path"])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    for s in packet["sessions"]:
        titles = s.get("window_titles") or [None]
        lab = label_by_block.get((s["app_name"], titles[0]))
        if lab is None:
            lab = next((v for (a, _), v in label_by_block.items() if a == s["app_name"]), None)
        lab = lab or {"capability": "other", "low_value": False, "friction": None}
        s["label"] = {
            "capability": lab["capability"], "task": None, "activity": None,
            "category": None, "low_value": lab["low_value"], "friction": lab["friction"],
        }
    packet["opportunities"] = OPPORTUNITIES
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    result = commit_mod.commit(cfg, store, packet_path)

    save_digest(store, generate_digest(cfg, store, person_id=pid))
    store.close()

    print(f"Seeded {n_frames:,} frames over {len(days)} days -> {cfg.db_path}")
    print(f"Analysis: {result}")
    print("Next:  fde_audit --config demo/config.toml dashboard")


if __name__ == "__main__":
    main()
