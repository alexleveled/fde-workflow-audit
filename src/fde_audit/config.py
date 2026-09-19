"""Load and validate config.toml (tomllib, stdlib) and resolve paths.

Paths under [paths] are resolved relative to data_dir unless absolute.
data_dir itself is resolved relative to the config file's directory unless absolute.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG_NAME = "config.toml"


@dataclass(frozen=True)
class CaptureCfg:
    interval_seconds: float = 5.0
    idle_threshold_seconds: float = 60.0
    idle_heartbeat_seconds: float = 60.0
    passive_reading: bool = True
    passive_grace_seconds: float = 300.0
    active_monitor_only: bool = True


@dataclass(frozen=True)
class ImageCfg:
    format: str = "jpeg"          # "jpeg" | "png"
    jpeg_quality: int = 80
    dupe_hash_size: int = 16
    dupe_hamming_threshold: int = 6


@dataclass(frozen=True)
class AnalysisCfg:
    session_max_minutes: float = 10.0        # N-minute cap: no summary spans hours
    session_idle_gap_seconds: float = 30.0   # gap between work frames past this => new session
    session_hash_jump_bits: int = 40         # frame_hash hamming > this inside same app/title => new session
    long_session_minutes: float = 20.0       # >= this gets start/mid/end vision reads, else one
    max_titles_per_session: int = 6          # distinct titles/domains recorded per session
    pingpong_min_switches: int = 4           # A-B-A-B run needs >= this many switches to flag
    ngram_top: int = 10                      # top app-switch bigrams to report
    ritual_min_days: int = 2                 # a trigram must recur across >= this many days
    observation_confirm: int = 3             # >= this many occurrences confirms an observation


@dataclass(frozen=True)
class DigestCfg:
    default_period: str = "weekly"           # cadence label on generated digests
    top_focus: int = 3                       # # of "recommended focus" items surfaced
    loaded_hourly_rate: Optional[float] = None  # if set, dollarize weekly hours


@dataclass(frozen=True)
class MonitorCfg:
    # A device is "capturing" (up) if its most recent frame is within this many
    # seconds. Must exceed the idle heartbeat (a running-but-idle machine still
    # writes at most one row per idle_heartbeat_seconds) plus a tick of slack, so
    # the default is derived from the capture cadence when left unset.
    live_window_seconds: float = 90.0
    # How often the dashboard's background monitor re-evaluates liveness and
    # records up/down transitions into the status log.
    sweep_seconds: float = 30.0


@dataclass(frozen=True)
class RetentionCfg:
    screenshot_days: int = 14


@dataclass(frozen=True)
class PrivacyCfg:
    app_denylist: tuple[str, ...] = ()
    title_denylist: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdentityCfg:
    default_department: str = "developer"


@dataclass(frozen=True)
class Config:
    config_path: Path
    data_dir: Path
    db_path: Path
    screenshots_dir: Path
    pause_file: Path
    capture: CaptureCfg
    image: ImageCfg
    analysis: AnalysisCfg
    digest: DigestCfg
    monitor: MonitorCfg
    retention: RetentionCfg
    privacy: PrivacyCfg
    identity: IdentityCfg
    raw: dict = field(default_factory=dict, repr=False)


def _resolve(base: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (base / p)


def load_config(config_path: str | Path | None = None) -> Config:
    """Load config from an explicit path, else ./config.toml (cwd), else the
    packaged default alongside the project root."""
    if config_path is not None:
        path = Path(config_path)
    else:
        cwd_cfg = Path.cwd() / DEFAULT_CONFIG_NAME
        # project root is two parents up from this file (src/fde_audit/config.py)
        pkg_cfg = Path(__file__).resolve().parents[2] / DEFAULT_CONFIG_NAME
        path = cwd_cfg if cwd_cfg.exists() else pkg_cfg

    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    cfg_dir = path.parent
    paths = data.get("paths", {})
    data_dir = _resolve(cfg_dir, paths.get("data_dir", "./digest_data")).resolve()
    db_path = _resolve(data_dir, paths.get("db_file", "digest.db"))
    screenshots_dir = _resolve(data_dir, paths.get("screenshots_dir", "screenshots"))
    pause_file = _resolve(data_dir, paths.get("pause_file", "PAUSE"))

    cap = data.get("capture", {})
    img = data.get("image", {})
    ana = data.get("analysis", {})
    dig = data.get("digest", {})
    mon = data.get("monitor", {})
    ret = data.get("retention", {})
    priv = data.get("privacy", {})
    ident = data.get("identity", {})

    image = ImageCfg(
        format=str(img.get("format", "jpeg")).lower(),
        jpeg_quality=int(img.get("jpeg_quality", 80)),
        dupe_hash_size=int(img.get("dupe_hash_size", 16)),
        dupe_hamming_threshold=int(img.get("dupe_hamming_threshold", 6)),
    )
    if image.format not in ("jpeg", "png"):
        raise ValueError(f"image.format must be 'jpeg' or 'png', got {image.format!r}")

    return Config(
        config_path=path,
        data_dir=data_dir,
        db_path=db_path,
        screenshots_dir=screenshots_dir,
        pause_file=pause_file,
        capture=CaptureCfg(
            interval_seconds=float(cap.get("interval_seconds", 5)),
            idle_threshold_seconds=float(cap.get("idle_threshold_seconds", 60)),
            idle_heartbeat_seconds=float(cap.get("idle_heartbeat_seconds", 60)),
            passive_reading=bool(cap.get("passive_reading", True)),
            passive_grace_seconds=float(cap.get("passive_grace_seconds", 300)),
            active_monitor_only=bool(cap.get("active_monitor_only", True)),
        ),
        image=image,
        analysis=AnalysisCfg(
            session_max_minutes=float(ana.get("session_max_minutes", 10)),
            session_idle_gap_seconds=float(ana.get("session_idle_gap_seconds", 30)),
            session_hash_jump_bits=int(ana.get("session_hash_jump_bits", 40)),
            long_session_minutes=float(ana.get("long_session_minutes", 20)),
            max_titles_per_session=int(ana.get("max_titles_per_session", 6)),
            pingpong_min_switches=int(ana.get("pingpong_min_switches", 4)),
            ngram_top=int(ana.get("ngram_top", 10)),
            ritual_min_days=int(ana.get("ritual_min_days", 2)),
            observation_confirm=int(ana.get("observation_confirm", 3)),
        ),
        digest=DigestCfg(
            default_period=str(dig.get("default_period", "weekly")),
            top_focus=int(dig.get("top_focus", 3)),
            loaded_hourly_rate=(float(dig["loaded_hourly_rate"])
                                if dig.get("loaded_hourly_rate") is not None else None),
        ),
        monitor=MonitorCfg(
            # When unset, derive a window that comfortably clears the idle
            # heartbeat cadence so a running-but-idle machine never reads as down.
            live_window_seconds=float(mon.get(
                "live_window_seconds",
                max(90.0, float(cap.get("idle_heartbeat_seconds", 60)) * 1.5
                    + float(cap.get("interval_seconds", 5))),
            )),
            sweep_seconds=float(mon.get("sweep_seconds", 30)),
        ),
        retention=RetentionCfg(screenshot_days=int(ret.get("screenshot_days", 14))),
        privacy=PrivacyCfg(
            app_denylist=tuple(s.lower() for s in priv.get("app_denylist", [])),
            title_denylist=tuple(s.lower() for s in priv.get("title_denylist", [])),
        ),
        identity=IdentityCfg(default_department=str(ident.get("default_department", "developer"))),
        raw=data,
    )
