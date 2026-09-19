"""Synthetic frame generator for exercising the Phase 2 analysis pass without a
real recording. Builds a small, realistic scenario: a recurring morning ritual,
an Excel<->QuickBooks ping-pong (manual data transfer), a reading block, and a
monolithic app with an internal screen change (hash jump).

Used by the tests and by the end-to-end demo. Deterministic (no randomness).
"""

from __future__ import annotations

from pathlib import Path

from fde_audit.db.store import Store, new_id

HASH_LEN = 64            # hex chars for a 16x16 (256-bit) average-hash
H_A = "0" * HASH_LEN
H_B = "f" * HASH_LEN     # hamming(H_A, H_B) = 256 -> a large jump
H_C = "1" * HASH_LEN     # hamming(H_A, H_C) = 64 -> also a jump; distinct picture

DAY_MS = 86_400_000
INTERVAL_MS = 5_000


def seed_people(store: Store) -> tuple[str, str]:
    pid = store.add_person(display_name="Alex", department="developer", created_at=1)
    did = store.upsert_device(pid, hostname="TESTHOST", os_name="Windows", tz="UTC", created_at=1)
    return pid, did


def _frame(pid, did, *, ts, app, title, state="active", idle_ms=0,
           frame_hash=H_A, screenshot_path=None, dup_of=None, local_day="2026-07-13",
           url_domain=None):
    return {
        "id": new_id(), "person_id": pid, "device_id": did, "ts_utc": ts,
        "local_day": local_day, "tz_offset_min": 0, "app_name": app,
        "exe_path": f"C:\\{app}.exe", "window_title": title, "url_domain": url_domain,
        "idle_ms": idle_ms, "activity_state": state, "is_heartbeat": 0,
        "screenshot_path": screenshot_path, "dup_of": dup_of, "frame_hash": frame_hash,
        "pruned": 0, "screen_w": 1920, "screen_h": 1080, "created_at": ts,
    }


def seed_frames(store: Store, pid: str, did: str, *, screenshots_dir: Path | None = None) -> int:
    """Insert the scenario. If screenshots_dir is given, writes tiny placeholder
    image files so representative-frame resolution finds real paths."""
    frames: list[dict] = []
    t = 1_783_900_000_000       # base epoch ms

    def img(rel: str) -> str:
        if screenshots_dir is not None:
            p = Path(screenshots_dir) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_bytes(b"\xff\xd8\xff\xe0placeholder")  # not a real JPEG; path-only
        return f"screenshots/{rel}"

    # --- Day 1 morning ritual: Outlook -> Excel -> Portal ---
    frames.append(_frame(pid, did, ts=t, app="Outlook", title="Inbox",
                         screenshot_path=img("d1/outlook.jpg"), local_day="2026-07-13"))
    t += INTERVAL_MS
    frames.append(_frame(pid, did, ts=t, app="Excel", title="report.xlsx",
                         screenshot_path=img("d1/excel1.jpg"), local_day="2026-07-13"))
    t += INTERVAL_MS
    frames.append(_frame(pid, did, ts=t, app="Chrome", title="Portal", url_domain="portal.acme.com",
                         screenshot_path=img("d1/portal.jpg"), local_day="2026-07-13"))

    # --- Excel <-> QuickBooks ping-pong (6 switches) ---
    t += INTERVAL_MS
    pp_apps = ["Excel", "QuickBooks", "Excel", "QuickBooks", "Excel", "QuickBooks", "Excel"]
    for i, app in enumerate(pp_apps):
        frames.append(_frame(pid, did, ts=t, app=app, title=f"{app} window",
                             idle_ms=0, screenshot_path=img(f"d1/pp{i}.jpg"),
                             local_day="2026-07-13"))
        t += INTERVAL_MS

    # --- Reading block in Chrome (low input density): 5 frames, high idle_ms ---
    read_img = img("d1/read.jpg")
    frames.append(_frame(pid, did, ts=t, app="Chrome", title="Docs — long article",
                         url_domain="docs.example.com", state="passive", idle_ms=40_000,
                         screenshot_path=read_img, local_day="2026-07-13"))
    read_frame_id = frames[-1]["id"]
    t += INTERVAL_MS
    for _ in range(4):
        frames.append(_frame(pid, did, ts=t, app="Chrome", title="Docs — long article",
                             url_domain="docs.example.com", state="passive", idle_ms=45_000,
                             screenshot_path=None, dup_of=read_frame_id,
                             local_day="2026-07-13"))
        t += INTERVAL_MS

    # --- Monolithic app with an internal screen change (hash jump, same title) ---
    frames.append(_frame(pid, did, ts=t, app="ERP", title="ERP",
                         frame_hash=H_A, screenshot_path=img("d1/erp1.jpg"),
                         local_day="2026-07-13"))
    t += INTERVAL_MS
    frames.append(_frame(pid, did, ts=t, app="ERP", title="ERP",
                         frame_hash=H_B, screenshot_path=img("d1/erp2.jpg"),
                         local_day="2026-07-13"))  # big hash jump -> new session

    # --- Idle gap, then Day 2 repeats the ritual (so it recurs on 2 days) ---
    t += DAY_MS
    frames.append(_frame(pid, did, ts=t, app="Outlook", title="Inbox",
                         screenshot_path=img("d2/outlook.jpg"), local_day="2026-07-14"))
    t += INTERVAL_MS
    frames.append(_frame(pid, did, ts=t, app="Excel", title="report.xlsx",
                         screenshot_path=img("d2/excel.jpg"), local_day="2026-07-14"))
    t += INTERVAL_MS
    frames.append(_frame(pid, did, ts=t, app="Chrome", title="Portal", url_domain="portal.acme.com",
                         screenshot_path=img("d2/portal.jpg"), local_day="2026-07-14"))

    for f in frames:
        store.insert_frame(f)
    return len(frames)
