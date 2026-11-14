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


@lru_cache
def get_settings() -> Settings:
    return Settings()
