"""Transition mining (model-free, Phase 2). Run BEFORE any vision read.

Free math over `frames`/sessions finds the anomalies; the expensive model only
explains them. Four signals:

  * app-switch bigrams — which app-to-app transitions happen most;
  * A-B-A-B ping-pong loops — rapid alternation between two apps is manual data
    transfer, the strongest integrate/build-custom signal;
  * recurring daily rituals — a sequence like Outlook -> Excel -> Portal every
    morning is a report ritual = an automation candidate;
  * per-session interaction mode from input density — typing *into* an app
    (data-entry -> automate) vs reading it (analysis -> a different opportunity).

Output is JSON-friendly dicts consumed by prepare.py to steer vision at hotspots
and by commit.py to seed typed observations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .sessionize import Session


@dataclass
class AppBlock:
    """Consecutive sessions sharing one app, merged. Ping-pong and rituals reason
    over app *blocks*, not raw sessions, so a same-app title change doesn't look
    like an app switch."""
    app: Optional[str]
    session_indexes: list[int]
    seconds: float
    ts_start: int
    ts_end: int


@dataclass
class MiningResult:
    app_bigrams: list[dict] = field(default_factory=list)
    pingpong: list[dict] = field(default_factory=list)
    rituals: list[dict] = field(default_factory=list)
    top_apps: list[dict] = field(default_factory=list)
    interaction_modes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "app_bigrams": self.app_bigrams,
            "pingpong": self.pingpong,
            "rituals": self.rituals,
            "top_apps": self.top_apps,
            "interaction_modes": self.interaction_modes,
        }


def _blocks(sessions: list[Session]) -> list[AppBlock]:
    blocks: list[AppBlock] = []
    for s in sessions:
        if blocks and blocks[-1].app == s.app_name:
            b = blocks[-1]
            b.session_indexes.append(s.index)
            b.seconds += s.work_seconds
            b.ts_end = s.ts_end
        else:
            blocks.append(AppBlock(
                app=s.app_name, session_indexes=[s.index],
                seconds=s.work_seconds, ts_start=s.ts_start, ts_end=s.ts_end,
            ))
    return blocks


def mine(
    sessions: list[Session],
    *,
    pingpong_min_switches: int = 4,
    ngram_top: int = 10,
    ritual_min_days: int = 2,
) -> MiningResult:
    result = MiningResult()
    if not sessions:
        return result

    blocks = _blocks(sessions)
    result.app_bigrams = _bigrams(blocks, ngram_top)
    result.pingpong = _pingpong(blocks, sessions, pingpong_min_switches)
    result.rituals = _rituals(sessions, ritual_min_days)
    result.top_apps = _top_apps(sessions)
    result.interaction_modes = _interaction_modes(sessions)
    return result


def _bigrams(blocks: list[AppBlock], top: int) -> list[dict]:
    counts: dict[tuple, int] = {}
    for a, b in zip(blocks, blocks[1:]):
        if a.app and b.app and a.app != b.app:
            key = (a.app, b.app)
            counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top]
    return [{"from": k[0], "to": k[1], "count": c} for k, c in ranked]


def _pingpong(blocks, sessions, min_switches) -> list[dict]:
    """Find maximal runs where app blocks alternate between exactly two apps.
    A run of L blocks = L-1 switches. Aggregate by the unordered {a,b} pair."""
    sec_by_index = {s.index: s.work_seconds for s in sessions}
    runs: list[dict] = []
    i = 0
    n = len(blocks)
    while i < n - 2:
        a, b = blocks[i].app, blocks[i + 1].app
        if not a or not b or a == b:
            i += 1
            continue
        # extend the alternating run a,b,a,b,...
        j = i + 1
        while j + 1 < n and blocks[j + 1].app == (a if (j + 1 - i) % 2 == 0 else b):
            j += 1
        run_blocks = blocks[i:j + 1]
        switches = len(run_blocks) - 1
        if switches >= min_switches:
            idxs = [idx for blk in run_blocks for idx in blk.session_indexes]
            seconds = sum(sec_by_index.get(k, 0) for k in idxs)
            runs.append({
                "app_a": a, "app_b": b, "switches": switches,
                "blocks": len(run_blocks),
                "est_minutes": round(seconds / 60.0, 1),
                "ts_start": run_blocks[0].ts_start,
                "ts_end": run_blocks[-1].ts_end,
                "session_indexes": idxs,
            })
            i = j + 1
        else:
            i += 1

    # aggregate runs sharing the same unordered pair
    agg: dict[frozenset, dict] = {}
    for r in runs:
        key = frozenset((r["app_a"], r["app_b"]))
        cur = agg.get(key)
        if cur is None:
            agg[key] = {**r, "runs": 1}
        else:
            cur["switches"] += r["switches"]
            cur["blocks"] += r["blocks"]
            cur["est_minutes"] = round(cur["est_minutes"] + r["est_minutes"], 1)
            cur["runs"] += 1
            cur["session_indexes"] += r["session_indexes"]
            cur["ts_end"] = max(cur["ts_end"], r["ts_end"])
    return sorted(agg.values(), key=lambda d: d["switches"], reverse=True)


def _rituals(sessions: list[Session], min_days: int) -> list[dict]:
    """App-block trigrams that recur across >= min_days distinct days = a routine
    likely worth automating end to end."""
    # per-day ordered app-block sequence
    by_day: dict[str, list[str]] = {}
    for s in sessions:
        seq = by_day.setdefault(s.local_day, [])
        if not seq or seq[-1] != s.app_name:
            if s.app_name:
                seq.append(s.app_name)

    trigram_days: dict[tuple, set] = {}
    for day, seq in by_day.items():
        for a, b, c in zip(seq, seq[1:], seq[2:]):
            if len({a, b, c}) == 3:            # three distinct apps in a row
                trigram_days.setdefault((a, b, c), set()).add(day)

    out = [
        {"sequence": list(k), "days": sorted(v), "day_count": len(v)}
        for k, v in trigram_days.items()
        if len(v) >= min_days
    ]
    return sorted(out, key=lambda d: d["day_count"], reverse=True)


def _top_apps(sessions: list[Session]) -> list[dict]:
    secs: dict[Optional[str], float] = {}
    for s in sessions:
        secs[s.app_name] = secs.get(s.app_name, 0.0) + s.work_seconds
    ranked = sorted(secs.items(), key=lambda kv: kv[1], reverse=True)
    return [{"app": a, "minutes": round(sec / 60.0, 1)} for a, sec in ranked if a]


def _interaction_modes(sessions: list[Session]) -> list[dict]:
    """Bucket each session by input density so vision knows whether it's watching
    data-entry (automate) or reading/analysis (a different opportunity)."""
    out = []
    for s in sessions:
        mode = "input-heavy" if s.input_density >= 0.5 else "reading"
        out.append({
            "session_index": s.index, "app": s.app_name,
            "input_density": s.input_density, "mode": mode,
            "minutes": round(s.work_seconds / 60.0, 1),
        })
    return out
