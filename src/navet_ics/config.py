"""Environment-driven configuration. No values are hardcoded in business logic."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key, "").strip()
    return value or default


def _env_int(key: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_float(key: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    """Runtime settings, resolved once at startup."""

    # Upstream: the Convex deployment that backs ifinavet.no.
    convex_url: str = field(default_factory=lambda: _env_str("CONVEX_URL", "https://gallant-pheasant-518.convex.cloud"))
    site_url: str = field(default_factory=lambda: _env_str("SITE_URL", "https://ifinavet.no"))

    # Upstream call behaviour.
    http_timeout: float = field(default_factory=lambda: _env_float("HTTP_TIMEOUT", 15.0, minimum=1.0, maximum=120.0))
    http_retries: int = field(default_factory=lambda: _env_int("HTTP_RETRIES", 3, minimum=0, maximum=10))
    http_backoff: float = field(default_factory=lambda: _env_float("HTTP_BACKOFF", 1.5, minimum=0.1, maximum=30.0))
    user_agent: str = field(
        default_factory=lambda: _env_str("USER_AGENT", "navet-calendar-proxy/1.0 (+https://github.com/vuhnger)")
    )

    # Refresh loop.
    refresh_interval: int = field(
        default_factory=lambda: _env_int("REFRESH_INTERVAL_SECONDS", 3600, minimum=300, maximum=86_400)
    )
    refresh_jitter: int = field(
        default_factory=lambda: _env_int("REFRESH_JITTER_SECONDS", 120, minimum=0, maximum=3600)
    )

    # Feed contents.
    calendar_name: str = field(default_factory=lambda: _env_str("CALENDAR_NAME", "Navet - arrangementer"))
    calendar_description: str = field(
        default_factory=lambda: _env_str(
            "CALENDAR_DESCRIPTION", "Arrangementer fra Navet, linjeforeningen for informatikk ved UiO."
        )
    )
    timezone: str = field(default_factory=lambda: _env_str("CALENDAR_TIMEZONE", "Europe/Oslo"))
    default_duration_minutes: int = field(
        default_factory=lambda: _env_int("DEFAULT_DURATION_MINUTES", 120, minimum=5, maximum=1440)
    )
    past_days: int = field(default_factory=lambda: _env_int("PAST_DAYS", 180, minimum=0, maximum=1825))
    # Peoply rejects a sync above 500 expanded events; stay under that ceiling.
    max_events: int = field(default_factory=lambda: _env_int("MAX_EVENTS", 400, minimum=1, maximum=10_000))

    # Persistence: last good feed survives restarts so we never serve an empty calendar.
    state_dir: str = field(default_factory=lambda: _env_str("STATE_DIR", "/var/lib/navet-ics"))

    # Serving.
    stale_after: int = field(
        default_factory=lambda: _env_int("STALE_AFTER_SECONDS", 21_600, minimum=600, maximum=604_800)
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
