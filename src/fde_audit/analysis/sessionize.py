"""Multi-signal sessionization (model-free, Phase 2).

A *window-session* is a contiguous span of work frames in the same working
context. Title-only splitting misses the apps that matter most — QuickBooks,
Excel, EMRs — whose window titles never change while the actual task does. So a
new session starts on ANY of:

  * app change
  * window-title / URL-domain change
  * a large frame_hash jump (the screen materially changed inside a static-title
    app — this is what decomposes monolithic apps)
  * an idle gap past a small tolerance (a multi-second time gap between
    consecutive *work* frames means idle frames were excluded between them)
  * an N-minute cap, so no single summary spans hours

Idle frames are excluded entirely (the caller passes work frames only). Output is
a list of Session objects with the signals the mining + vision stages need,
including which already-on-disk frames to look at (dups resolve to their image).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..capture.screenshot import hamming_distance

WORK_STATES = ("active", "passive")


@dataclass
class RepFrame:
    """A representative frame the vision pass should read. `path` is the stored
    (data_dir-relative) screenshot path; for a near-duplicate frame it is the
    resolved path of the image it dup-links to."""
    frame_id: str
    path: str
    position: str          # start | mid | end
    ts_utc: int


@dataclass
class Session:
    index: int
    frame_ids: list[str]
    ts_start: int
    ts_end: int
    app_name: Optional[str]
    window_titles: list[str]
    url_domains: list[str]
    frame_count: int
    span_seconds: float
    work_seconds: float
    input_density: float
    boundary_reason: str
    local_day: str
    representatives: list[RepFrame] = field(default_factory=list)

    @property
    def frame_id_start(self) -> str:
        return self.frame_ids[0]

    @property
    def frame_id_end(self) -> str:
        return self.frame_ids[-1]


def _val(row: Any, key: str) -> Any:
    """Row accessor that works for sqlite3.Row and plain dicts (tests)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def sessionize(
    frames: list[Any],
    *,
    interval_seconds: float = 5.0,
    max_minutes: float = 10.0,
    idle_gap_seconds: float = 30.0,
    hash_jump_bits: int = 40,
    long_session_minutes: float = 20.0,
    max_titles: int = 6,
) -> list[Session]:
    """Split an ordered list of work frames into window-sessions.

    `frames` must be ordered by ts_utc ascending and contain only work
    (active/passive) frames — idle frames excluded. Each element is a mapping
    with at least: id, ts_utc, app_name, window_title, url_domain, idle_ms,
    frame_hash, screenshot_path, dup_of, pruned, local_day.
    """
    if not frames:
        return []

    # frame_id -> on-disk image path (only frames that actually wrote an image
    # and weren't pruned). Used to resolve a dup frame to its image.
    image_path: dict[str, str] = {}
    for f in frames:
        sp = _val(f, "screenshot_path")
        if sp and not _val(f, "pruned"):
            image_path[_val(f, "id")] = sp

    max_ms = max_minutes * 60_000
    gap_ms = idle_gap_seconds * 1000

    sessions: list[Session] = []
    cur: list[Any] = []
    cur_reason = "first"

    def flush(reason_of_first: str) -> None:
        if cur:
            sessions.append(
                _build_session(
                    len(sessions), cur, reason_of_first,
                    interval_seconds, long_session_minutes, max_titles, image_path,
                )
            )

    prev: Optional[Any] = None
    for f in frames:
        if prev is None:
            cur = [f]
            cur_reason = "first"
            prev = f
            continue

        reason = _boundary(prev, f, cur[0], gap_ms, max_ms, hash_jump_bits)
        if reason:
            flush(cur_reason)
            cur = [f]
            cur_reason = reason
        else:
            cur.append(f)
        prev = f

    flush(cur_reason)
    return sessions


def _boundary(prev, cur, session_start, gap_ms, max_ms, hash_jump_bits) -> Optional[str]:
    """Return the boundary reason if `cur` starts a new session vs `prev`, else
    None. Checked in priority order so the recorded reason is deterministic."""
    if _val(cur, "app_name") != _val(prev, "app_name"):
        return "app-change"
    if _val(cur, "window_title") != _val(prev, "window_title"):
        return "title-change"
    if _val(cur, "url_domain") != _val(prev, "url_domain"):
        return "url-change"
    gap = _val(cur, "ts_utc") - _val(prev, "ts_utc")
    if gap > gap_ms:
        return "idle-gap"
    if _val(cur, "ts_utc") - _val(session_start, "ts_utc") > max_ms:
        return "time-cap"
    ha, hb = _val(prev, "frame_hash"), _val(cur, "frame_hash")
    if ha and hb and hamming_distance(ha, hb) > hash_jump_bits:
        return "hash-jump"
    return None


def _dominant_app(frames: list[Any]) -> Optional[str]:
    counts: dict[Optional[str], int] = {}
    for f in frames:
        a = _val(f, "app_name")
        counts[a] = counts.get(a, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _distinct(frames: list[Any], key: str, cap: int) -> list[str]:
    out: list[str] = []
    for f in frames:
        v = _val(f, key)
        if v and v not in out:
            out.append(v)
            if len(out) >= cap:
                break
    return out


def _build_session(
    index, frames, boundary_reason, interval_seconds,
    long_session_minutes, max_titles, image_path,
) -> Session:
    ts_start = _val(frames[0], "ts_utc")
    ts_end = _val(frames[-1], "ts_utc")
    n = len(frames)
    span_seconds = (ts_end - ts_start) / 1000.0

    # input density: fraction of ticks with input within the last tick.
    tick_ms = interval_seconds * 1000
    with_input = sum(1 for f in frames if (_val(f, "idle_ms") or 0) <= tick_ms)
    input_density = with_input / n if n else 0.0

    session = Session(
        index=index,
        frame_ids=[_val(f, "id") for f in frames],
        ts_start=ts_start,
        ts_end=ts_end,
        app_name=_dominant_app(frames),
        window_titles=_distinct(frames, "window_title", max_titles),
        url_domains=_distinct(frames, "url_domain", max_titles),
        frame_count=n,
        span_seconds=span_seconds,
        work_seconds=n * interval_seconds,
        input_density=round(input_density, 3),
        boundary_reason=boundary_reason,
        local_day=_val(frames[0], "local_day"),
    )
    session.representatives = _pick_representatives(
        frames, image_path, span_seconds, long_session_minutes
    )
    return session


def _pick_representatives(frames, image_path, span_seconds, long_session_minutes) -> list[RepFrame]:
    """Choose the frames the vision pass reads. Only frames whose image is on
    disk qualify; a dup frame resolves to the image it links to. Long sessions
    get start/mid/end (a mid-session task change isn't missed); short ones get
    one. De-duplicated by resolved image path so we never pay to read the same
    picture twice."""
    def resolve(f) -> Optional[str]:
        fid = _val(f, "id")
        if fid in image_path:
            return image_path[fid]
        dup = _val(f, "dup_of")
        if dup and dup in image_path:
            return image_path[dup]
        return None

    with_img = [(f, resolve(f)) for f in frames]
    with_img = [(f, p) for (f, p) in with_img if p]
    if not with_img:
        return []

    long = span_seconds >= long_session_minutes * 60
    if long and len(with_img) >= 3:
        picks = [
            (with_img[0], "start"),
            (with_img[len(with_img) // 2], "mid"),
            (with_img[-1], "end"),
        ]
    else:
        # one read: prefer the middle (most representative of steady state)
        picks = [(with_img[len(with_img) // 2], "mid")]

    reps: list[RepFrame] = []
    seen_paths: set[str] = set()
    for (f, path), position in picks:
        if path in seen_paths:
            continue
        seen_paths.add(path)
        reps.append(RepFrame(
            frame_id=_val(f, "id"), path=path, position=position,
            ts_utc=_val(f, "ts_utc"),
        ))
    return reps
