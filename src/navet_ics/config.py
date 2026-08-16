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


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_choice(key: str, default: str, allowed: set[str]) -> str:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    if raw not in allowed:
        raise ValueError(f"{key} must be one of {sorted(allowed)}, got {raw!r}")
    return raw


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{key} must be one of {sorted(_TRUE | _FALSE)}, got {raw!r}")


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

    # How many upstream calls may be in flight at once during a refresh. The
    # enrichment steps below fan out per company and per event, and this is
    # somebody else's Convex deployment: stay a polite client.
    upstream_concurrency: int = field(
        default_factory=lambda: _env_int("UPSTREAM_CONCURRENCY", 4, minimum=1, maximum=16)
    )

    # Enrichment. Each of these costs extra upstream calls per refresh, so each
    # can be turned off independently if Navet's backend ever needs the quiet.
    #
    # Organizer lookups are the expensive one: one query per event, every
    # refresh, because upstream exposes no bulk organizer query.
    fetch_organizers: bool = field(default_factory=lambda: _env_bool("FETCH_ORGANIZERS", True))
    max_organizer_lookups: int = field(
        default_factory=lambda: _env_int("MAX_ORGANIZER_LOOKUPS", 250, minimum=0, maximum=5_000)
    )
    # Organizer e-mail addresses are personal data about Navet volunteers that
    # upstream happens to expose. Republishing them on a public endpoint is a
    # decision for whoever runs this service, so it is off unless asked for.
    include_organizer_emails: bool = field(default_factory=lambda: _env_bool("INCLUDE_ORGANIZER_EMAILS", False))
    fetch_companies: bool = field(default_factory=lambda: _env_bool("FETCH_COMPANIES", True))
    fetch_job_listings: bool = field(default_factory=lambda: _env_bool("FETCH_JOB_LISTINGS", True))

    # Feed contents.
    calendar_name: str = field(default_factory=lambda: _env_str("CALENDAR_NAME", "Navet - arrangementer"))
    calendar_description: str = field(
        default_factory=lambda: _env_str(
            "CALENDAR_DESCRIPTION", "Arrangementer fra Navet, linjeforeningen for informatikk ved UiO."
        )
    )
    registration_calendar_name: str = field(
        default_factory=lambda: _env_str("REGISTRATION_CALENDAR_NAME", "Navet - påmeldingsåpninger")
    )
    registration_calendar_description: str = field(
        default_factory=lambda: _env_str(
            "REGISTRATION_CALENDAR_DESCRIPTION", "Når påmeldingen åpner for Navets arrangementer."
        )
    )
    jobs_calendar_name: str = field(default_factory=lambda: _env_str("JOBS_CALENDAR_NAME", "Navet - søknadsfrister"))
    jobs_calendar_description: str = field(
        default_factory=lambda: _env_str("JOBS_CALENDAR_DESCRIPTION", "Søknadsfrister for stillinger utlyst hos Navet.")
    )
    timezone: str = field(default_factory=lambda: _env_str("CALENDAR_TIMEZONE", "Europe/Oslo"))
    default_duration_minutes: int = field(
        default_factory=lambda: _env_int("DEFAULT_DURATION_MINUTES", 120, minimum=5, maximum=1440)
    )
    # Registration openings and application deadlines are instants, not blocks.
    # They still need a non-zero length or some clients render nothing at all.
    reminder_duration_minutes: int = field(
        default_factory=lambda: _env_int("REMINDER_DURATION_MINUTES", 30, minimum=1, maximum=1440)
    )
    # Minutes of VALARM lead time in the reminder feeds. 0 emits no alarm.
    reminder_alarm_minutes: int = field(
        default_factory=lambda: _env_int("REMINDER_ALARM_MINUTES", 15, minimum=0, maximum=10_080)
    )
    past_days: int = field(default_factory=lambda: _env_int("PAST_DAYS", 180, minimum=0, maximum=1825))
    jobs_past_days: int = field(default_factory=lambda: _env_int("JOBS_PAST_DAYS", 180, minimum=0, maximum=1825))
    # Peoply rejects a sync above 500 expanded events; stay under that ceiling.
    max_events: int = field(default_factory=lambda: _env_int("MAX_EVENTS", 400, minimum=1, maximum=10_000))
    max_jobs: int = field(default_factory=lambda: _env_int("MAX_JOBS", 400, minimum=1, maximum=10_000))
    # Smallest fraction of the previous feed a refresh may shrink to before it is
    # treated as upstream breakage rather than as real deletions. 0 disables it.
    min_event_ratio: float = field(default_factory=lambda: _env_float("MIN_EVENT_RATIO", 0.5, minimum=0.0, maximum=1.0))

    # Company logos, emitted as RFC 7986 IMAGE. Resolving one costs a query per
    # company whose logo we have not seen before, plus a HEAD to learn the media
    # type. Both are cached on identifiers that change when the logo changes.
    event_images: bool = field(default_factory=lambda: _env_bool("EVENT_IMAGES", True))
    # Media types worth emitting. SVG is deliberately absent: several clients
    # (and Peoply's importer) will not render it, so pointing them at one is
    # worse than emitting no image at all.
    image_types: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            part.strip().lower()
            for part in _env_str("IMAGE_TYPES", "image/jpeg,image/png,image/webp").split(",")
            if part.strip()
        )
    )

    # Notifications. A refresh that turns up a new job listing, or an event
    # whose registration has just opened, posts one message per record to this
    # webhook. Empty disables the whole thing; the Atom feed still works.
    notify_webhook_url: str = field(default_factory=lambda: _env_str("NOTIFY_WEBHOOK_URL", ""))
    # `auto` picks slack/discord from the URL's host and falls back to a generic
    # JSON body. Set it explicitly for anything that proxies those hosts.
    notify_webhook_format: str = field(
        default_factory=lambda: _env_choice("NOTIFY_WEBHOOK_FORMAT", "auto", {"auto", "slack", "discord", "json"})
    )
    notify_new_jobs: bool = field(default_factory=lambda: _env_bool("NOTIFY_NEW_JOBS", True))
    notify_registration_open: bool = field(default_factory=lambda: _env_bool("NOTIFY_REGISTRATION_OPEN", True))
    # How far back a registration opening still counts as news. This is what
    # stops a newly loaded semester from announcing openings from months ago;
    # it needs to comfortably exceed the refresh interval and nothing more.
    notify_registration_window_hours: int = field(
        default_factory=lambda: _env_int("NOTIFY_REGISTRATION_WINDOW_HOURS", 48, minimum=1, maximum=8_760)
    )
    # Ceiling on messages per refresh, so a bulk upstream import cannot turn
    # into a hundred pings in a channel.
    notify_max_items: int = field(default_factory=lambda: _env_int("NOTIFY_MAX_ITEMS", 10, minimum=1, maximum=100))
    notify_timeout: float = field(default_factory=lambda: _env_float("NOTIFY_TIMEOUT", 10.0, minimum=1.0, maximum=60.0))

    # Atom feed. Titled for what it carries (new postings) rather than reusing
    # the calendar's name, which is about deadlines.
    jobs_feed_title: str = field(default_factory=lambda: _env_str("JOBS_FEED_TITLE", "Navet - stillingsannonser"))
    feed_max_items: int = field(default_factory=lambda: _env_int("FEED_MAX_ITEMS", 50, minimum=1, maximum=1_000))

    # JSON API paging.
    default_page_size: int = field(default_factory=lambda: _env_int("DEFAULT_PAGE_SIZE", 50, minimum=1, maximum=1_000))
    max_page_size: int = field(default_factory=lambda: _env_int("MAX_PAGE_SIZE", 200, minimum=1, maximum=5_000))

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
