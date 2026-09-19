"""`digest` CLI.

Commands:
  digest db init                          create schema (tables + indexes)
  digest person add --name .. --dept ..   seed a person UUID (+ register device)
  digest person list
  digest device register                  register this machine for a person
  digest capture start [--minutes N]      run the watcher loop
  digest capture supervise                keep capture alive (restart-on-crash)
  digest capture install-task             auto-start capture at logon (Windows task)
  digest capture status                   show recent-frame / paused / disk stats
  digest status                           fleet liveness: which machines are capturing now
  digest report / observe ...             ranked opportunity report + confirmation (Phase 3)
  digest digest generate|list|show        cadence Markdown digest from the ledger (Phase 4)
  digest brief prepare|commit             executive brief: packet out, Claude Code writes
                                          the narrative, commit saves it as a digest
  digest rollup                           (re)build app_usage_rollup (free, no model)
  digest dashboard [--host --port]        serve the reporting dashboard (Phase 5, beta)
  digest prune [--days N]                 delete old screenshot files
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Optional

from . import clock
from .config import Config, load_config
from .db.store import Store
from .platform import get_platform, os_name


def _store(cfg: Config) -> Store:
    return Store(cfg.db_path)


def _fmt_dur(ms: Optional[int]) -> str:
    """Human 'ago'/duration from a millisecond span (e.g. 3123000 -> '52m')."""
    if ms is None:
        return "never"
    s = int(ms // 1000)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


def _resolve_person(store: Store, person_id: Optional[str]):
    if person_id:
        p = store.get_person(person_id)
        if not p:
            sys.exit(f"no person with id {person_id}")
        return p
    p = store.first_person()
    if not p:
        sys.exit("no people yet — run `digest person add` first")
    return p


def _register_device(cfg: Config, store: Store, person_id: str) -> str:
    plat = get_platform()
    return store.upsert_device(
        person_id=person_id,
        hostname=plat.hostname(),
        os_name=os_name(),
        tz=plat.tz_name(),
        created_at=clock.now_ms(),
    )


# -- commands -------------------------------------------------------------
def cmd_db_init(cfg: Config, args) -> None:
    with _store(cfg) as store:
        store.init_db()
    print(f"schema initialized at {cfg.db_path}")


def cmd_person_add(cfg: Config, args) -> None:
    with _store(cfg) as store:
        store.init_db()
        pid = store.add_person(
            display_name=args.name,
            department=args.dept or cfg.identity.default_department,
            created_at=clock.now_ms(),
            email=args.email,
        )
        did = _register_device(cfg, store, pid)
    print(f"added person {args.name} ({pid}), department={args.dept or cfg.identity.default_department}")
    print(f"registered device {did}")


def cmd_person_list(cfg: Config, args) -> None:
    with _store(cfg) as store:
        for p in store.list_people():
            print(f"{p['person_id']}  {p['display_name']:20}  {p['department']:12}  active={p['active']}")


def cmd_device_register(cfg: Config, args) -> None:
    with _store(cfg) as store:
        p = _resolve_person(store, args.person)
        did = _register_device(cfg, store, p["person_id"])
    print(f"device {did} registered for {p['display_name']}")


def _resolve_capture_person(cfg: Config, store: Store, person_id: Optional[str]):
    """Pick the person to record for. An explicit --person wins. Otherwise
    auto-enroll from the OS identity (SID) + department (Active Directory, else
    the packaged config default) — the fleet path, so a department push needs no
    manual `person add`. Returns (person_row, device_id)."""
    if person_id:
        p = _resolve_person(store, person_id)
        return p, _register_device(cfg, store, p["person_id"])
    from . import enroll
    return enroll.auto_enroll(cfg, store, get_platform())


def cmd_capture_start(cfg: Config, args) -> None:
    from .capture.watcher import Watcher

    with _store(cfg) as store:
        store.init_db()
        p, did = _resolve_capture_person(cfg, store, args.person)
        watcher = Watcher(cfg, store, p["person_id"], did)
        try:
            if args.minutes:
                _run_bounded(watcher, args.minutes)
            else:
                watcher.start()
        except RuntimeError as e:
            sys.exit(str(e))


def _run_bounded(watcher, minutes: float) -> None:
    """Run the watcher for a fixed duration (used for the 2-minute verification)."""
    import threading

    timer = threading.Timer(minutes * 60, watcher.stop)
    timer.daemon = True
    timer.start()
    try:
        watcher.start()
    finally:
        timer.cancel()


def cmd_capture_status(cfg: Config, args) -> None:
    from pathlib import Path

    with _store(cfg) as store:
        store.init_db()
        total = store.query_one("SELECT COUNT(*) AS c FROM frames")["c"]
        with_img = store.query_one(
            "SELECT COUNT(*) AS c FROM frames WHERE screenshot_path IS NOT NULL")["c"]
        dupes = store.query_one("SELECT COUNT(*) AS c FROM frames WHERE dup_of IS NOT NULL")["c"]
        idle = store.query_one("SELECT COUNT(*) AS c FROM frames WHERE activity_state='idle'")["c"]
        last = store.query_one("SELECT * FROM frames ORDER BY ts_utc DESC LIMIT 1")
    print(f"frames: {total}  (with image: {with_img}, dup-linked: {dupes}, idle: {idle})")
    if last:
        print(f"last frame: {clock.local_day(last['ts_utc'])} "
              f"app={last['app_name']} state={last['activity_state']}")
    paused = cfg.pause_file.exists()
    print(f"paused: {paused} (pause file: {cfg.pause_file})")
    # disk
    sdir = Path(cfg.screenshots_dir)
    if sdir.exists():
        total_bytes = sum(f.stat().st_size for f in sdir.rglob("*") if f.is_file())
        print(f"screenshot bytes on disk: {total_bytes:,} ({total_bytes/1e6:.1f} MB)")


def cmd_status(cfg: Config, args) -> None:
    """Fleet liveness: which machines are capturing right now, plus the recent
    up/down status log. Records any transitions since the last check (so running
    this — or the dashboard — keeps the log current)."""
    from . import monitor

    with _store(cfg) as store:
        store.init_db()
        monitor.sweep(store, cfg)                 # keep the transition log fresh
        snap = monitor.evaluate(store, cfg)
        events = monitor.status_events(store, limit=args.log)

    sm = snap["summary"]
    win = int(cfg.monitor.live_window_seconds)
    print(f"Capture status — {sm['capturing']} of {sm['total']} machine(s) capturing "
          f"(live window {win}s)")
    if not snap["devices"]:
        print("  no devices registered yet — run `fde_audit capture start` on a machine first.")
    for d in snap["devices"]:
        dot = "UP  " if d["status"] == "up" else "DOWN"
        ago = _fmt_dur(d["silent_ms"])
        app = f" · {d['last_app']}" if d["last_app"] else ""
        print(f"  [{dot}] {d['hostname']:18} {d['person'] or '—':14} "
              f"last frame {ago} ago{app} · {d['frames_24h']} frames/24h")

    if events:
        print("\nRecent status changes (newest first):")
        for e in events:
            when = clock.local_day(e["ts"]) if e["ts"] else "—"
            hhmm = time.strftime("%H:%M", time.localtime(e["ts"] / 1000)) if e["ts"] else ""
            arrow = "came UP" if e["status"] == "up" else "went DOWN"
            gap = f" after {_fmt_dur(e['gap_ms'])} silent" if e["gap_ms"] else ""
            print(f"  {when} {hhmm}  {e['hostname'] or e['device_id'][:8]:18} {arrow}{gap}")


def cmd_capture_supervise(cfg: Config, args) -> None:
    """Keep `capture start` alive: relaunch it if it dies unexpectedly (a crash,
    an installer stealing focus, an OS kill). A clean stop (Ctrl+C, or --minutes
    elapsing) exits without restart. This is the durable way to run capture; pair
    it with `capture install-task` to start it at logon with no terminal open."""
    import subprocess

    base = [sys.executable, "-m", "fde_audit"]
    if getattr(args, "config", None):
        base += ["--config", str(args.config)]
    child_cmd = base + ["capture", "start"]
    if args.person:
        child_cmd += ["--person", args.person]

    min_backoff, max_backoff = 2.0, 60.0
    backoff = min_backoff
    print(f"supervising capture (restart-on-crash, backoff {int(min_backoff)}–{int(max_backoff)}s; "
          f"Ctrl+C to stop). child: {' '.join(child_cmd)}")
    while True:
        start = time.time()
        proc = subprocess.Popen(child_cmd)
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            print("\nstop requested — shutting the child down")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return
        if rc == 0:
            print("capture exited cleanly — not restarting")
            return
        # ran a healthy while before dying => reset the backoff; otherwise grow it
        if time.time() - start >= max_backoff:
            backoff = min_backoff
        print(f"capture exited with code {rc}; restarting in {int(backoff)}s")
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            print("\nstop requested during backoff")
            return
        backoff = min(max_backoff, backoff * 2)


_TASK_NAME = "FDE Audit Capture"


def cmd_capture_install_task(cfg: Config, args) -> None:
    """Register a Windows Scheduled Task that runs `capture supervise` at logon,
    in the background (no console window) and restarts it on failure — so an
    installer or a closed terminal can't leave capture silently dead."""
    import subprocess
    import tempfile
    from pathlib import Path as _Path
    from xml.sax.saxutils import escape

    if sys.platform != "win32":
        sys.exit("install-task is Windows-only (macOS launchd support comes with the rollout)")

    # pythonw.exe runs with no console window; fall back to python.exe.
    exe = _Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    cfg_arg = f' --config &quot;{escape(str(cfg.config_path))}&quot;'
    person_arg = f" --person {escape(args.person)}" if args.person else ""
    args_line = f"-m fde_audit{cfg_arg} capture supervise{person_arg}"

    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>FDE Audit capture supervisor (auto-start + restart)</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
  </Settings>
  <Actions>
    <Exec>
      <Command>{escape(str(runner))}</Command>
      <Arguments>{args_line}</Arguments>
      <WorkingDirectory>{escape(str(cfg.config_path.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    tmp = _Path(tempfile.gettempdir()) / "fde_audit_task.xml"
    tmp.write_text(xml, encoding="utf-16")
    cmd = ["schtasks", "/Create", "/TN", _TASK_NAME, "/XML", str(tmp), "/F"]
    print(f"registering scheduled task {_TASK_NAME!r} (runs at logon):")
    print("  " + " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        sys.exit(f"schtasks failed: {e}. You can register it manually with the XML at {tmp}")
    print(f"done. it starts at next logon; start it now with:  schtasks /Run /TN \"{_TASK_NAME}\"")
    print(f"check it:   schtasks /Query /TN \"{_TASK_NAME}\"")
    print(f"remove it:  schtasks /Delete /TN \"{_TASK_NAME}\" /F")


def cmd_rollup(cfg: Config, args) -> None:
    from .rollup import build_rollup

    with _store(cfg) as store:
        store.init_db()
        n = build_rollup(cfg, store)
    print(f"rollup upserted {n} (person, device, day, app) rows")


def cmd_analyze_prepare(cfg: Config, args) -> None:
    from .analysis.prepare import prepare

    with _store(cfg) as store:
        store.init_db()
        p = _resolve_person(store, args.person)
        info = prepare(
            cfg, store, p["person_id"],
            since=args.since, all_frames=args.all, model=args.model,
            out_path=args.out,
        )
    if info["frame_count"] == 0:
        print("no unanalyzed work frames in range — nothing to prepare.")
        print("(record a session first, or pass --all to re-analyze everything.)")
        return
    print(f"run {info['run_id']} prepared")
    print(f"  {info['frame_count']} work frames -> {info['session_count']} sessions "
          f"({info['pingpong']} ping-pong loops, {info['rituals']} rituals)")
    print(f"  packet: {info['packet_path']}")
    print()
    print("Next (Claude Code, vision): read each session's representative_frames, "
          "fill its `label`, add any `opportunities`, then run:")
    print(f"  digest analyze commit --packet {info['packet_path']}")


def _work_frames_for(cfg: Config, store: Store, args):
    p = _resolve_person(store, args.person)
    if args.all:
        since = None
    elif args.since is not None:
        since = args.since
    else:
        wm = store.analysis_watermark(p["person_id"])
        since = wm + 1 if wm is not None else None
    return p, store.work_frames(p["person_id"], ts_start=since)


def cmd_analyze_sessions(cfg: Config, args) -> None:
    from .analysis.sessionize import sessionize

    ac = cfg.analysis
    with _store(cfg) as store:
        store.init_db()
        _, frames = _work_frames_for(cfg, store, args)
        sessions = sessionize(
            frames, interval_seconds=cfg.capture.interval_seconds,
            max_minutes=ac.session_max_minutes,
            idle_gap_seconds=ac.session_idle_gap_seconds,
            hash_jump_bits=ac.session_hash_jump_bits,
            long_session_minutes=ac.long_session_minutes,
            max_titles=ac.max_titles_per_session,
        )
    print(f"{len(frames)} work frames -> {len(sessions)} sessions")
    for s in sessions:
        title = (s.window_titles[0] if s.window_titles else "")[:48]
        print(f"  #{s.index:<3} {clock.local_day(s.ts_start)} {s.app_name or '?':16} "
              f"{s.frame_count:>3}f {s.work_seconds/60:>5.1f}m dens={s.input_density:.2f} "
              f"[{s.boundary_reason:11}] reads={len(s.representatives)}  {title}")


def cmd_analyze_mine(cfg: Config, args) -> None:
    from .analysis.mining import mine
    from .analysis.sessionize import sessionize

    ac = cfg.analysis
    with _store(cfg) as store:
        store.init_db()
        _, frames = _work_frames_for(cfg, store, args)
        sessions = sessionize(
            frames, interval_seconds=cfg.capture.interval_seconds,
            max_minutes=ac.session_max_minutes,
            idle_gap_seconds=ac.session_idle_gap_seconds,
            hash_jump_bits=ac.session_hash_jump_bits,
            long_session_minutes=ac.long_session_minutes,
            max_titles=ac.max_titles_per_session,
        )
        m = mine(sessions, pingpong_min_switches=ac.pingpong_min_switches,
                 ngram_top=ac.ngram_top, ritual_min_days=ac.ritual_min_days)
    print("top apps by time:")
    for a in m.top_apps[:10]:
        print(f"  {a['app']:20} {a['minutes']:>6.1f}m")
    print("app-switch bigrams:")
    for b in m.app_bigrams:
        print(f"  {b['from']:16} -> {b['to']:16} x{b['count']}")
    print("ping-pong loops (integrate/build-custom signal):")
    for pp in m.pingpong:
        print(f"  {pp['app_a']} <-> {pp['app_b']}  {pp['switches']} switches, "
              f"~{pp['est_minutes']}m")
    print("recurring rituals (automation candidate):")
    for r in m.rituals:
        print(f"  {' -> '.join(r['sequence'])}  on {r['day_count']} day(s)")


def cmd_analyze_commit(cfg: Config, args) -> None:
    from .analysis.commit import commit

    with _store(cfg) as store:
        store.init_db()
        result = commit(cfg, store, args.packet)
    print(f"committed run {result['run_id']}: {result['summaries']} summaries, "
          f"{result['observations']} observations")


def cmd_analyze_status(cfg: Config, args) -> None:
    with _store(cfg) as store:
        store.init_db()
        p = _resolve_person(store, args.person)
        pid = p["person_id"]
        runs = store.query(
            "SELECT status, COUNT(*) AS c FROM analysis_runs GROUP BY status")
        summ = store.query_one(
            "SELECT COUNT(*) AS c FROM summaries WHERE person_id = ?", (pid,))["c"]
        wm = store.analysis_watermark(pid)
        caps = store.query(
            "SELECT capability, COUNT(*) AS c FROM summaries"
            " WHERE person_id = ? AND capability IS NOT NULL"
            " GROUP BY capability ORDER BY c DESC", (pid,))
        obs = store.query(
            "SELECT status, opportunity_type, COUNT(*) AS c,"
            "       SUM(est_minutes_per_week) AS mins FROM observations"
            " WHERE person_id = ? GROUP BY status, opportunity_type", (pid,))
        top_obs = store.query(
            "SELECT pattern_key, opportunity_type, status, evidence_count,"
            "       est_minutes_per_week, suggested_upgrade FROM observations"
            " WHERE person_id = ? ORDER BY COALESCE(est_minutes_per_week,0) DESC LIMIT 8",
            (pid,))
    print(f"person: {p['display_name']} ({pid})")
    print("runs: " + ", ".join(f"{r['status']}={r['c']}" for r in runs) if runs else "runs: none")
    print(f"summaries: {summ}   watermark: {wm}")
    if caps:
        print("capabilities: " + ", ".join(f"{c['capability']}={c['c']}" for c in caps))
    if obs:
        print("observations:")
        for o in obs:
            mins = f", ~{o['mins']:.0f}m/wk" if o["mins"] else ""
            print(f"  {o['status']:10} {o['opportunity_type'] or '?':12} x{o['c']}{mins}")
    if top_obs:
        print("top opportunities:")
        for o in top_obs:
            mins = f"~{o['est_minutes_per_week']:.0f}m/wk" if o["est_minutes_per_week"] else "n/a"
            print(f"  [{o['opportunity_type'] or '?':11}|{o['status']:9}|{mins:>10}] "
                  f"{o['pattern_key']}")
            if o["suggested_upgrade"]:
                print(f"      -> {o['suggested_upgrade']}")


def cmd_report(cfg: Config, args) -> None:
    import json as _json

    from .reporting import DEFAULT_STATUSES, build_report, render_text

    with _store(cfg) as store:
        store.init_db()
        person_id = None
        if not args.all_people:
            p = _resolve_person(store, args.person)
            person_id = p["person_id"]
        statuses = None if args.all_status else (
            [args.status] if args.status else list(DEFAULT_STATUSES))
        report = build_report(
            cfg, store,
            person_id=person_id,
            department=args.department,
            statuses=statuses,
            opportunity_types=[args.type] if args.type else None,
            min_minutes=args.min_minutes,
            limit=args.limit,
        )
    if args.json:
        print(_json.dumps(report, indent=2))
    else:
        print(render_text(report))


def cmd_observe_list(cfg: Config, args) -> None:
    with _store(cfg) as store:
        store.init_db()
        person_id = None
        if not args.all_people:
            person_id = _resolve_person(store, args.person)["person_id"]
        rows = store.list_observations(
            person_id=person_id,
            statuses=None if args.all_status else None if args.status is None else [args.status],
            order="minutes", limit=args.limit,
        )
    if not rows:
        print("no observations yet — run `digest analyze` first.")
        return
    for r in rows:
        mins = f"~{r['est_minutes_per_week']:.0f}m/wk" if r["est_minutes_per_week"] else "n/a"
        print(f"{r['id'][:8]}  [{r['opportunity_type'] or '?':11}|{r['status']:9}|{mins:>10}] "
              f"evid={r['evidence_count'] or 0:<3} {r['pattern_key']}")


def _apply_status(cfg: Config, args, status: str, *, recompute: bool = False) -> None:
    with _store(cfg) as store:
        store.init_db()
        person_id = None
        if not args.all_people:
            person_id = _resolve_person(store, args.person)["person_id"]
        matches = store.find_observation(args.ref, person_id=person_id)
        if not matches:
            sys.exit(f"no observation matches {args.ref!r}")
        if len(matches) > 1:
            print(f"{args.ref!r} is ambiguous — {len(matches)} matches:")
            for m in matches:
                print(f"  {m['id'][:8]}  {m['pattern_key']}")
            sys.exit("re-run with a longer id prefix or the exact pattern_key")
        obs = matches[0]
        new_status = status
        if recompute:
            thresh = cfg.analysis.observation_confirm
            new_status = "confirmed" if (obs["evidence_count"] or 0) >= thresh else "open"
        store.set_observation_status(obs["id"], new_status)
    print(f"{obs['pattern_key']} ({obs['id'][:8]}): {obs['status']} -> {new_status}")


def cmd_observe_accept(cfg: Config, args) -> None:
    _apply_status(cfg, args, "actioned")


def cmd_observe_dismiss(cfg: Config, args) -> None:
    _apply_status(cfg, args, "dismissed")


def cmd_observe_reopen(cfg: Config, args) -> None:
    _apply_status(cfg, args, "open", recompute=True)


def cmd_digest_generate(cfg: Config, args) -> None:
    import json as _json
    from pathlib import Path

    from .digest import generate_digest, save_digest

    org_view = args.all_people or args.department
    with _store(cfg) as store:
        store.init_db()
        person_id = None
        if not org_view:
            person_id = _resolve_person(store, args.person)["person_id"]
        digest = generate_digest(
            cfg, store,
            period=args.period,
            person_id=person_id,
            department=args.department,
            min_minutes=args.min_minutes,
            limit=args.limit,
        )
        saved_id = None
        if not args.no_save and not org_view:
            saved_id = save_digest(store, digest)

    if args.json:
        # the report dict is JSON-ready; drop the nested duplicate content for size
        out = {k: v for k, v in digest.items() if k != "report"}
        out["report"] = digest["report"]
        if saved_id:
            out["digest_id"] = saved_id
        print(_json.dumps(out, indent=2))
        return

    if args.out:
        Path(args.out).write_text(digest["content_md"], encoding="utf-8")
        print(f"wrote {args.out} ({len(digest['content_md'])} bytes)")
    else:
        print(digest["content_md"])

    if saved_id:
        print(f"\n[saved digest {saved_id[:8]} — period={digest['period']}]")
    elif org_view:
        print("\n[org-wide view — not saved (digests are person-scoped); "
              "use --out to write a file]")


def cmd_digest_list(cfg: Config, args) -> None:
    with _store(cfg) as store:
        store.init_db()
        person_id = None
        if not args.all_people:
            person_id = _resolve_person(store, args.person)["person_id"]
        rows = store.list_digests(person_id=person_id, limit=args.limit)
    if not rows:
        print("no digests yet — run `digest digest generate`.")
        return
    for r in rows:
        print(f"{r['id'][:8]}  {clock.local_day(r['generated_at']):10}  "
              f"{(r['period'] or '?'):10}  {r['display_name']:16}  "
              f"tokens={r['token_cost'] or 0}")


def cmd_digest_show(cfg: Config, args) -> None:
    with _store(cfg) as store:
        store.init_db()
        person_id = None
        if not args.all_people:
            person_id = _resolve_person(store, args.person)["person_id"]
        matches = store.find_digest(args.ref, person_id=person_id)
        if not matches:
            sys.exit(f"no digest matches {args.ref!r}")
        if len(matches) > 1:
            print(f"{args.ref!r} is ambiguous — {len(matches)} matches:")
            for m in matches:
                print(f"  {m['id'][:8]}  {clock.local_day(m['generated_at'])}  {m['period']}")
            sys.exit("re-run with a longer id prefix")
        print(matches[0]["content_md"])


def cmd_brief_prepare(cfg: Config, args) -> None:
    from .brief import prepare_brief

    with _store(cfg) as store:
        store.init_db()
        p = _resolve_person(store, args.person)
        info = prepare_brief(
            cfg, store, p["person_id"], period=args.period,
            out_path=args.out,
        )
    if info["opportunity_count"] == 0:
        print("the opportunity ledger is empty — nothing to write a brief about.")
        print("(run capture -> analyze prepare -> label/commit first.)")
        return
    print(f"brief packet prepared ({info['opportunity_count']} opportunities, "
          f"period={info['period']})")
    print(f"  packet: {info['packet_path']}")
    print()
    print("Next (Claude Code): read the packet, write the narrative Markdown to")
    print(f"  {info['output_md_path']}")
    print("then run:")
    print(f"  digest brief commit --packet {info['packet_path']}")


def cmd_brief_commit(cfg: Config, args) -> None:
    from .brief import commit_brief

    with _store(cfg) as store:
        store.init_db()
        try:
            result = commit_brief(cfg, store, args.packet, md_path=args.md)
        except ValueError as e:
            sys.exit(str(e))
    print(f"saved executive brief {result['digest_id'][:8]} "
          f"(period={result['period']}, {result['bytes']} bytes) — "
          "it's in `digest digest list` and the dashboard's Saved reports.")


def cmd_dashboard(cfg: Config, args) -> None:
    try:
        from .dashboard import run as run_dashboard
    except ImportError:
        sys.exit(
            "the dashboard needs FastAPI + uvicorn — install them with:\n"
            "  pip install -e .[dashboard]\n"
            "(they are kept optional so the base capture install stays dependency-light)")
    # make sure the DB/schema exists before serving
    with _store(cfg) as store:
        store.init_db()
    print(f"FDE Audit dashboard -> http://{args.host}:{args.port}  (Ctrl+C to stop)")
    print(f"reading {cfg.db_path}")
    run_dashboard(cfg, host=args.host, port=args.port)


def cmd_prune(cfg: Config, args) -> None:
    from .retention import prune

    with _store(cfg) as store:
        store.init_db()
        result = prune(cfg, store, days=args.days)
    print(f"pruned {result['deleted']} files, freed {result['bytes_freed']:,} bytes "
          f"({result['missing']} already missing)")


# -- parser ---------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fde_audit", description="FDE Audit — a forward deployed engineer's eyes: watch-yourself-work capture and analysis")
    p.add_argument("--config", help="path to config.toml (default: ./config.toml or packaged)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    db = sub.add_parser("db", help="database operations")
    dbsub = db.add_subparsers(dest="subcmd", required=True)
    dbsub.add_parser("init", help="create schema").set_defaults(func=cmd_db_init)

    person = sub.add_parser("person", help="identity")
    psub = person.add_subparsers(dest="subcmd", required=True)
    padd = psub.add_parser("add", help="add a person (UUID + department)")
    padd.add_argument("--name", required=True)
    padd.add_argument("--dept", help="department (default from config)")
    padd.add_argument("--email")
    padd.set_defaults(func=cmd_person_add)
    psub.add_parser("list", help="list people").set_defaults(func=cmd_person_list)

    dev = sub.add_parser("device", help="devices")
    dsub = dev.add_subparsers(dest="subcmd", required=True)
    dreg = dsub.add_parser("register", help="register this machine")
    dreg.add_argument("--person", help="person id (default: first person)")
    dreg.set_defaults(func=cmd_device_register)

    cap = sub.add_parser("capture", help="the capture loop")
    csub = cap.add_subparsers(dest="subcmd", required=True)
    cstart = csub.add_parser("start", help="start the watcher")
    cstart.add_argument("--person", help="person id (default: first person)")
    cstart.add_argument("--minutes", type=float, help="run for N minutes then stop")
    cstart.set_defaults(func=cmd_capture_start)
    csup = csub.add_parser("supervise", help="keep capture alive (relaunch on crash)")
    csup.add_argument("--person", help="person id (default: first person)")
    csup.set_defaults(func=cmd_capture_supervise)
    citask = csub.add_parser("install-task",
                             help="register a Windows logon task that supervises capture")
    citask.add_argument("--person", help="person id (default: first person)")
    citask.set_defaults(func=cmd_capture_install_task)
    csub.add_parser("status", help="show capture status").set_defaults(func=cmd_capture_status)

    st = sub.add_parser("status", help="fleet liveness — which machines are capturing right now")
    st.add_argument("--log", type=int, default=10, help="how many recent status changes to show")
    st.set_defaults(func=cmd_status)

    ana = sub.add_parser("analyze", help="the analysis pass (Phase 2)")
    asub = ana.add_subparsers(dest="subcmd", required=True)

    aprep = asub.add_parser("prepare", help="sessionize + mine, write a packet for vision labeling")
    aprep.add_argument("--person", help="person id (default: first person)")
    aprep.add_argument("--since", type=int, help="only frames with ts_utc >= this (epoch ms)")
    aprep.add_argument("--all", action="store_true", help="ignore the watermark; analyze all frames")
    aprep.add_argument("--model", help="model label recorded on the run/summaries")
    aprep.add_argument("--out", help="packet output path (default: data_dir/analysis/packet_<run>.json)")
    aprep.set_defaults(func=cmd_analyze_prepare)

    asess = asub.add_parser("sessions", help="show sessionization (model-free, no run)")
    asess.add_argument("--person", help="person id (default: first person)")
    asess.add_argument("--since", type=int, help="epoch ms lower bound")
    asess.add_argument("--all", action="store_true", help="ignore the watermark")
    asess.set_defaults(func=cmd_analyze_sessions)

    amine = asub.add_parser("mine", help="show transition mining (model-free, no run)")
    amine.add_argument("--person", help="person id (default: first person)")
    amine.add_argument("--since", type=int, help="epoch ms lower bound")
    amine.add_argument("--all", action="store_true", help="ignore the watermark")
    amine.set_defaults(func=cmd_analyze_mine)

    acommit = asub.add_parser("commit", help="write a labeled packet (one transaction)")
    acommit.add_argument("--packet", required=True, help="path to the labeled packet json")
    acommit.set_defaults(func=cmd_analyze_commit)

    astat = asub.add_parser("status", help="runs / summaries / observations overview")
    astat.add_argument("--person", help="person id (default: first person)")
    astat.set_defaults(func=cmd_analyze_status)

    rep = sub.add_parser("report", help="ranked opportunity report (Phase 3)")
    rep.add_argument("--person", help="person id (default: first person)")
    rep.add_argument("--all-people", action="store_true", help="every person, not just one")
    rep.add_argument("--department", help="slice to one department")
    rep.add_argument("--type", help="filter to one opportunity_type")
    rep.add_argument("--status", help="filter to a single status")
    rep.add_argument("--all-status", action="store_true", help="include dismissed observations")
    rep.add_argument("--min-minutes", type=float, help="only opportunities >= this weekly cost")
    rep.add_argument("--limit", type=int, help="cap the number of opportunities shown")
    rep.add_argument("--json", action="store_true", help="emit the report as JSON")
    rep.set_defaults(func=cmd_report)

    obs = sub.add_parser("observe", help="inspect / confirm / dismiss observations (Phase 3)")
    osub = obs.add_subparsers(dest="subcmd", required=True)

    olist = osub.add_parser("list", help="list observations with ids")
    olist.add_argument("--person", help="person id (default: first person)")
    olist.add_argument("--all-people", action="store_true")
    olist.add_argument("--status", help="filter to a single status")
    olist.add_argument("--all-status", action="store_true", help="include dismissed")
    olist.add_argument("--limit", type=int)
    olist.set_defaults(func=cmd_observe_list)

    for verb, fn, helptext in (
        ("accept", cmd_observe_accept, "mark actioned (a real opportunity you're pursuing)"),
        ("dismiss", cmd_observe_dismiss, "mark dismissed (false positive / not relevant)"),
        ("reopen", cmd_observe_reopen, "clear a human status; recompute open/confirmed by evidence"),
    ):
        oc = osub.add_parser(verb, help=helptext)
        oc.add_argument("ref", help="observation id (or prefix) or pattern_key")
        oc.add_argument("--person", help="person id (default: first person)")
        oc.add_argument("--all-people", action="store_true", help="search across all people")
        oc.set_defaults(func=fn)

    dig = sub.add_parser("digest", help="generate / view cadence digests (Phase 4)")
    dsub2 = dig.add_subparsers(dest="subcmd", required=True)

    dgen = dsub2.add_parser("generate", help="build a Markdown digest from the current ledger")
    dgen.add_argument("--person", help="person id (default: first person)")
    dgen.add_argument("--all-people", action="store_true", help="org-wide view (not saved)")
    dgen.add_argument("--department", help="slice to one department (org view, not saved)")
    dgen.add_argument("--period", help="cadence label (default from config, e.g. 'weekly')")
    dgen.add_argument("--min-minutes", type=float, help="only opportunities >= this weekly cost")
    dgen.add_argument("--limit", type=int, help="cap the number of opportunities shown")
    dgen.add_argument("--out", help="write the Markdown to this file instead of stdout")
    dgen.add_argument("--json", action="store_true", help="emit the full digest as JSON")
    dgen.add_argument("--no-save", action="store_true", help="don't persist to the digests table")
    dgen.set_defaults(func=cmd_digest_generate)

    dlist = dsub2.add_parser("list", help="list saved digests")
    dlist.add_argument("--person", help="person id (default: first person)")
    dlist.add_argument("--all-people", action="store_true")
    dlist.add_argument("--limit", type=int)
    dlist.set_defaults(func=cmd_digest_list)

    dshow = dsub2.add_parser("show", help="print a saved digest's Markdown")
    dshow.add_argument("ref", help="digest id or prefix")
    dshow.add_argument("--person", help="person id (default: first person)")
    dshow.add_argument("--all-people", action="store_true", help="search across all people")
    dshow.set_defaults(func=cmd_digest_show)

    brf = sub.add_parser("brief", help="executive brief (narrative report, written by Claude Code)")
    bsub = brf.add_subparsers(dest="subcmd", required=True)

    bprep = bsub.add_parser("prepare", help="bundle ledger + period stats into a brief packet")
    bprep.add_argument("--person", help="person id (default: first person)")
    bprep.add_argument("--period", default="7d",
                       help="activity window: 1d/7d/30d/90d/all (default 7d)")
    bprep.add_argument("--out", help="packet output path (default: data_dir/analysis/brief_<id>.json)")
    bprep.set_defaults(func=cmd_brief_prepare)

    bcommit = bsub.add_parser("commit", help="save the written brief Markdown as a digest")
    bcommit.add_argument("--packet", required=True, help="path to the brief packet json")
    bcommit.add_argument("--md", help="override the Markdown path (default: from the packet)")
    bcommit.set_defaults(func=cmd_brief_commit)

    roll = sub.add_parser("rollup", help="(re)build app_usage_rollup")
    roll.set_defaults(func=cmd_rollup)

    dash = sub.add_parser("dashboard", help="serve the reporting dashboard (Phase 5, beta)")
    dash.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    dash.add_argument("--port", type=int, default=8787, help="bind port (default 8787)")
    dash.set_defaults(func=cmd_dashboard)

    pr = sub.add_parser("prune", help="delete old screenshot files")
    pr.add_argument("--days", type=int, help="retention days (default from config)")
    pr.set_defaults(func=cmd_prune)

    return p


def main(argv: Optional[list[str]] = None) -> None:
    # Digests are UTF-8 Markdown (arrows, em dashes); the Windows console defaults
    # to cp1252 and would choke on them. Files already write UTF-8 explicitly.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)
    args.func(cfg, args)


if __name__ == "__main__":
    main()
