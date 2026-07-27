"""Fetches Navet events from the public Convex backend that powers ifinavet.no.

ifinavet.no is a Next.js frontend over a Convex deployment. Its event queries are
public Convex queries, so we call them over Convex's HTTP query API instead of
scraping the rendered HTML (which sits behind Vercel bot protection and would
break on every markup change).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# Convex function paths (see ifinavet/yggdrasil: packages/backend/convex/events/queries.ts).
_Q_SEMESTERS = "events/queries:getPossibleSemesters"
_Q_ALL = "events/queries:getAll"

# Convex returns JSON numbers for timestamps in epoch milliseconds.
_MS = 1000


class UpstreamError(RuntimeError):
    """Raised when the upstream data could not be retrieved or made sense of."""


class _Transient(UpstreamError):
    """A failure worth retrying, as opposed to one that will repeat identically."""


@dataclass(frozen=True)
class NavetEvent:
    """A single normalized event, independent of the upstream representation."""

    uid: str
    title: str
    start: datetime
    teaser: str
    description_html: str
    location: str
    company: str
    food: str
    language: str
    age_restriction: str
    url: str
    external_url: str | None
    created: datetime
    participation_limit: int | None


def _as_int(value: Any) -> int | None:
    """Convex encodes numbers as JSON floats; coerce defensively."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class ConvexClient:
    """Minimal client for Convex's public query endpoint, with retries and timeouts."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.convex_url.rstrip("/"),
            timeout=httpx.Timeout(settings.http_timeout),
            headers={"User-Agent": settings.user_agent, "Content-Type": "application/json"},
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query(self, path: str, args: dict[str, Any]) -> Any:
        """Run a public Convex query, retrying transient failures with backoff."""
        attempts = self._settings.http_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            if attempt:
                delay = self._settings.http_backoff * (2 ** (attempt - 1))
                log.warning("retrying convex query %s in %.1fs (attempt %d/%d)", path, delay, attempt + 1, attempts)
                await asyncio.sleep(delay)
            try:
                response = await self._client.post("/api/query", json={"path": path, "args": args, "format": "json"})
                # 5xx and 429 are worth another attempt; a 4xx means we are asking
                # wrong and will keep asking wrong, so fail immediately with the
                # real status rather than burning the whole retry budget.
                if response.status_code >= 500 or response.status_code == 429:
                    raise _Transient(f"convex {path} returned HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise UpstreamError(f"convex {path} returned HTTP {response.status_code}")
                payload = response.json()
            except (httpx.TransportError, _Transient, ValueError) as exc:
                last_error = exc
                continue

            if not isinstance(payload, dict):
                raise UpstreamError(f"convex {path} returned a non-object payload")
            if payload.get("status") != "success":
                # A function-level error will not fix itself by retrying.
                raise UpstreamError(f"convex {path} failed: {payload.get('errorMessage', 'unknown error')}")
            return payload.get("value")

        raise UpstreamError(f"convex {path} unreachable after {attempts} attempts: {last_error}")


def _event_url(settings: Settings, raw: dict[str, Any]) -> str:
    """Public permalink. The site routes /events/<identifier> by slug or id."""
    identifier = _as_str(raw.get("slug")) or _as_str(raw.get("_id"))
    return f"{settings.site_url.rstrip('/')}/events/{identifier}"


def _normalize(settings: Settings, raw: dict[str, Any]) -> NavetEvent | None:
    """Convert one upstream record into a NavetEvent, or None if unusable."""
    event_id = _as_str(raw.get("_id"))
    start_ms = _as_int(raw.get("eventStart"))
    title = _as_str(raw.get("title"))

    # Without a stable id or a start time the entry cannot become a VEVENT.
    if not event_id or start_ms is None:
        log.warning("skipping event with missing id or start: %r", raw.get("title"))
        return None

    try:
        start = datetime.fromtimestamp(start_ms / _MS, tz=UTC)
    except (OverflowError, OSError, ValueError):
        log.warning("skipping event %s with unusable timestamp %r", event_id, start_ms)
        return None

    created_ms = _as_int(raw.get("_creationTime"))
    created = datetime.fromtimestamp(created_ms / _MS, tz=UTC) if created_ms is not None else start

    limit = _as_int(raw.get("participationLimit"))
    external = _as_str(raw.get("externalUrl")) or None

    return NavetEvent(
        uid=event_id,
        title=title or "Arrangement",
        start=start,
        teaser=_as_str(raw.get("teaser")),
        description_html=_as_str(raw.get("description")),
        location=_as_str(raw.get("location")),
        company=_as_str(raw.get("hostingCompanyName")),
        food=_as_str(raw.get("food")),
        language=_as_str(raw.get("language")),
        age_restriction=_as_str(raw.get("ageRestriction")),
        url=_event_url(settings, raw),
        external_url=external if external and external.startswith(("http://", "https://")) else None,
        created=created,
        participation_limit=limit if limit and limit > 0 else None,
    )


def _relevant_semesters(raw: Any, now: datetime) -> list[tuple[str, int]]:
    """Pick the semesters worth querying: last year through next year."""
    low, high = now.year - 1, now.year + 1
    seen: set[tuple[str, int]] = set()

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            year = _as_int(item.get("year"))
            semester = _as_str(item.get("semester"))
            if year is not None and semester and low <= year <= high:
                seen.add((semester, year))

    if not seen:
        # Upstream gave us nothing usable; fall back to a fixed, bounded sweep.
        seen = {(sem, year) for year in range(low, high + 1) for sem in ("vår", "høst")}

    return sorted(seen, key=lambda pair: (pair[1], pair[0]))


async def fetch_events(settings: Settings, client: ConvexClient) -> list[NavetEvent]:
    """Fetch every published event in the relevant window, newest data wins."""
    now = datetime.now(tz=UTC)
    semesters = _relevant_semesters(await client.query(_Q_SEMESTERS, {}), now)

    cutoff = now - timedelta(days=settings.past_days)
    by_uid: dict[str, NavetEvent] = {}
    failed: list[str] = []

    for semester, year in semesters:
        try:
            value = await client.query(_Q_ALL, {"semester": semester, "year": year})
        except UpstreamError as exc:
            # Isolate the failure: one permanently broken semester query must not
            # block refreshes for every other semester forever. The caller still
            # refuses to publish if nothing at all succeeded.
            log.error("semester %s %d failed: %s", semester, year, exc)
            failed.append(f"{semester} {year}")
            continue

        if not isinstance(value, dict):
            log.warning("unexpected payload for semester %s %d", semester, year)
            continue

        published = value.get("published")
        if not isinstance(published, list):
            continue

        for raw in published:
            if not isinstance(raw, dict) or raw.get("published") is not True:
                continue
            event = _normalize(settings, raw)
            if event is None or event.start < cutoff:
                continue
            by_uid[event.uid] = event

    # Every semester failing means the upstream is broken, not that Navet has no
    # events. Publishing that as an empty feed would archive real events downstream.
    if failed and len(failed) == len(semesters):
        raise UpstreamError(f"all {len(failed)} semester queries failed: {', '.join(failed)}")

    events = sorted(by_uid.values(), key=lambda e: (e.start, e.uid))
    if len(events) > settings.max_events:
        # Drop from the oldest end: upcoming events matter more than history.
        log.warning("truncating feed from %d to %d events", len(events), settings.max_events)
        events = events[-settings.max_events :]

    if not events and semesters:
        log.info("upstream returned no events in window (semesters checked: %d)", len(semesters))

    return events
