"""Application settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SEED_DIR = BASE_DIR / "seed"


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # Storage
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./data/crew_health.db")
    )

    # HTTP
    cors_origins: list[str] = field(default_factory=lambda: _list("CORS_ORIGINS", "*"))
    admin_api_key: str | None = field(default_factory=lambda: os.getenv("ADMIN_API_KEY") or None)

    # OSDR ingestion
    osdr_base_url: str = field(
        default_factory=lambda: os.getenv("OSDR_BASE_URL", "https://osdr.nasa.gov").rstrip("/")
    )
    osdr_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("OSDR_TIMEOUT_SECONDS", "60"))
    )
    # auto = try OSDR first and fall back to the bundled copy; remote = OSDR only; bundled = offline copy only
    ingest_source: str = field(default_factory=lambda: os.getenv("INGEST_SOURCE", "auto"))
    ingest_on_startup: bool = field(default_factory=lambda: _bool("INGEST_ON_STARTUP", True))
    raw_cache_dir: Path = field(
        default_factory=lambda: Path(os.getenv("RAW_CACHE_DIR", "./data/raw"))
    )

    # Analytics
    # A post-flight value is flagged "watch" when it moves at least this much
    # from the crew member's own pre-flight mean, in percent AND in z-score.
    watch_pct_change: float = field(
        default_factory=lambda: float(os.getenv("WATCH_PCT_CHANGE", "20"))
    )
    watch_z_score: float = field(default_factory=lambda: float(os.getenv("WATCH_Z_SCORE", "2.0")))
    # With only three pre-flight samples the SD is unstable, so it never goes
    # below this fraction of the baseline mean.
    baseline_sd_floor_fraction: float = field(
        default_factory=lambda: float(os.getenv("BASELINE_SD_FLOOR_FRACTION", "0.05"))
    )
    mission_duration_days: int = field(
        default_factory=lambda: int(os.getenv("MISSION_DURATION_DAYS", "3"))
    )

    # Synthetic long-duration mission (daily vitals)
    synthetic_on_startup: bool = field(default_factory=lambda: _bool("SYNTHETIC_ON_STARTUP", True))
    synthetic_seed: int = field(default_factory=lambda: int(os.getenv("SYNTHETIC_SEED", "42")))
    # Simulated "today" of the mission; default: the last day of the synthetic dataset.
    current_mission_day: int | None = field(
        default_factory=lambda: int(os.environ["CURRENT_MISSION_DAY"]) if os.getenv("CURRENT_MISSION_DAY") else None
    )

    # Authentication
    jwt_secret: str | None = field(default_factory=lambda: os.getenv("JWT_SECRET") or None)
    jwt_expire_minutes: int = field(default_factory=lambda: int(os.getenv("JWT_EXPIRE_MINUTES", "720")))
    seed_demo_users: bool = field(default_factory=lambda: _bool("SEED_DEMO_USERS", True))
    demo_password: str = field(default_factory=lambda: os.getenv("DEMO_PASSWORD", "spaceapps2026"))

    # Pattern detection, proactive messages and human support
    scheduler_enabled: bool = field(default_factory=lambda: _bool("SCHEDULER_ENABLED", True))
    detection_interval_minutes: float = field(
        default_factory=lambda: float(os.getenv("DETECTION_INTERVAL_MINUTES", "60"))
    )
    proactive_min_interval_days: int = field(
        default_factory=lambda: int(os.getenv("PROACTIVE_MIN_INTERVAL_DAYS", "7"))
    )
    support_webhook_url: str | None = field(default_factory=lambda: os.getenv("SUPPORT_WEBHOOK_URL") or None)

    # PDF knowledge base
    knowledge_max_upload_mb: float = field(
        default_factory=lambda: float(os.getenv("KNOWLEDGE_MAX_UPLOAD_MB", "20"))
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
