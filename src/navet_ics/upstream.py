"""Fetches Navet data from the public Convex backend that powers ifinavet.no.

ifinavet.no is a Next.js frontend over a Convex deployment. Its event, company
and job-listing queries are public Convex queries, so we call them over Convex's
HTTP query API instead of scraping the rendered HTML (which sits behind Vercel
bot protection and would break on every markup change).

Everything this module returns is normalized away from the upstream shape, so
the rest of the service never sees a Convex field name.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# Convex function paths (see ifinavet/yggdrasil: packages/backend/convex/).
_Q_SEMESTERS = "events/queries:getPossibleSemesters"
_Q_ALL = "events/queries:getAll"
_Q_EVENT = "events/queries:getEvent"
_Q_COMPANIES = "companies/queries:getAll"
_Q_COMPANY = "companies/queries:getById"
_Q_JOBS = "jobListings/queries:getAll"

# Convex returns JSON numbers for timestamps in epoch milliseconds.
_MS = 1000


class UpstreamError(RuntimeError):
    """Raised when the upstream data could not be retrieved or made sense of."""


class _Transient(UpstreamError):
    """A failure worth retrying, as opposed to one that will repeat identically."""


@dataclass(frozen=True)
class NavetOrganizer:
    """A person responsible for an event."""

    id: str
    name: str
    role: str
    image_url: str | None
    # Only populated when INCLUDE_ORGANIZER_EMAILS is on; see Settings.
    email: str | None


@dataclass(frozen=True)
class NavetCompany:
    """A company in Navet's register, with its logo resolved where possible."""

    id: str
    name: str
    description_html: str
    org_number: int | None
    main_sponsor: bool
    logo_id: str | None
    image_url: str | None
    image_type: str | None


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
    company_id: str = ""
    registration_opens: datetime | None = None
    slug: str | None = None
    has_registration_form: bool = False
    image_url: str | None = None
    image_type: str | None = None
    organizers: tuple[NavetOrganizer, ...] = ()


@dataclass(frozen=True)
class NavetJobListing:
    """A job advertised through ifinavet.no, whose deadline is the calendar event."""

    uid: str
    title: str
    kind: str
    teaser: str
    description_html: str
    application_url: str | None
    deadline: datetime
    company_id: str
    company: str
    url: str
    created: datetime
    image_url: str | None = None
    image_type: str | None = None


@dataclass(frozen=True)
class Dataset:
    """Everything one refresh pulled from upstream."""

    events: list[NavetEvent] = field(default_factory=list)
    companies: list[NavetCompany] = field(default_factory=list)
    job_listings: list[NavetJobListing] = field(default_factory=list)


@dataclass
class UpstreamCaches:
    """Cross-refresh caches for the enrichment lookups.

    Both are keyed on something that changes when the underlying value changes:
    a company's logo id changes when its logo is replaced, and a Convex storage
    URL is per uploaded file. So a hit is always still correct, and the steady
    state costs no extra requests at all.
    """

    logo_url_by_logo_id: dict[str, str | None] = field(default_factory=dict)
    media_type_by_url: dict[str, str | None] = field(default_factory=dict)


def _as_int(value: Any) -> int | None:
    """Convex encodes numbers as JSON floats; coerce defensively."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_time(value: Any) -> datetime | None:
    """Epoch milliseconds to an aware datetime, or None if unusable."""
    ms = _as_int(value)
    if ms is None or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / _MS, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _as_http_url(value: Any) -> str | None:
    text = _as_str(value)
    return text if text.startswith(("http://", "https://")) else None


class ConvexClient:
    """Minimal client for Convex's public query endpoint, with retries and timeouts."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.convex_url.rstrip("/"),
            timeout=httpx.Timeout(settings.http_timeout),
            headers={"User-Agent": settings.user_agent, "Content-Type": "application/json"},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
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

    async def media_type(self, url: str) -> str | None:
        """The Content-Type of an upstream asset, or None if it cannot be learned.

        The URL comes from upstream data rather than from us, so it is pinned to
        the configured Convex host before being requested: a compromised or
        buggy backend must not be able to point this service at an arbitrary
        address. Redirects stay off for the same reason — pinning only the first
        hop would let a 302 walk us straight off the host we just checked, and
        Convex storage serves these directly anyway. Failures are not retried —
        an image is a nice-to-have, and the refresh must not slow down or fail
        over one.
        """
        if urlsplit(url).netloc != urlsplit(self._settings.convex_url).netloc:
            log.warning("ignoring off-host asset url %s", url)
            return None
        try:
            response = await self._client.head(url)
        except httpx.HTTPError as exc:
            log.warning("could not probe asset %s: %s", url, exc)
            return None
        if response.status_code >= 400:
            log.warning("asset %s returned HTTP %d", url, response.status_code)
            return None
        return response.headers.get("content-type", "").split(";")[0].strip().lower() or None


async def _gather_limited(settings: Settings, tasks: list[Any]) -> list[Any]:
    """Run coroutines with a bounded number in flight, preserving order."""
    semaphore = asyncio.Semaphore(settings.upstream_concurrency)

    async def guarded(coro: Any) -> Any:
        async with semaphore:
            return await coro

    return await asyncio.gather(*(guarded(task) for task in tasks), return_exceptions=True)


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

    start = _as_time(start_ms)
    if start is None:
        log.warning("skipping event %s with unusable timestamp %r", event_id, start_ms)
        return None

    created = _as_time(raw.get("_creationTime")) or start
    limit = _as_int(raw.get("participationLimit"))

    return NavetEvent(
        uid=event_id,
        title=title or "Arrangement",
        start=start,
        teaser=_as_str(raw.get("teaser")),
        description_html=_as_str(raw.get("description")),
        location=_as_str(raw.get("location")),
        company=_as_str(raw.get("hostingCompanyName")),
        company_id=_as_str(raw.get("hostingCompany")),
        food=_as_str(raw.get("food")),
        language=_as_str(raw.get("language")),
        age_restriction=_as_str(raw.get("ageRestriction")),
        url=_event_url(settings, raw),
        external_url=_as_http_url(raw.get("externalUrl")),
        created=created,
        participation_limit=limit if limit and limit > 0 else None,
        registration_opens=_as_time(raw.get("registrationOpens")),
        slug=_as_str(raw.get("slug")) or None,
        has_registration_form=bool(_as_str(raw.get("formId"))),
    )


def _normalize_company(raw: dict[str, Any]) -> NavetCompany | None:
    company_id = _as_str(raw.get("_id"))
    if not company_id:
        return None
    return NavetCompany(
        id=company_id,
        name=_as_str(raw.get("name")) or "Ukjent bedrift",
        description_html=_as_str(raw.get("description")),
        org_number=_as_int(raw.get("orgNumber")),
        main_sponsor=raw.get("mainSponsor") is True,
        logo_id=_as_str(raw.get("logo")) or None,
        image_url=_as_http_url(raw.get("imageUrl")),
        image_type=None,
    )


def _normalize_job(settings: Settings, raw: dict[str, Any]) -> NavetJobListing | None:
    listing_id = _as_str(raw.get("_id"))
    deadline = _as_time(raw.get("deadline"))
    if not listing_id or deadline is None:
        log.warning("skipping job listing with missing id or deadline: %r", raw.get("title"))
        return None

    return NavetJobListing(
        uid=listing_id,
        title=_as_str(raw.get("title")) or "Stilling",
        kind=_as_str(raw.get("type")),
        teaser=_as_str(raw.get("teaser")),
        description_html=_as_str(raw.get("description")),
        application_url=_as_http_url(raw.get("applicationUrl")),
        deadline=deadline,
        company_id=_as_str(raw.get("company")),
        company=_as_str(raw.get("companyName")) or "Ukjent bedrift",
        url=f"{settings.site_url.rstrip('/')}/job/{listing_id}",
        created=_as_time(raw.get("_creationTime")) or deadline,
        image_url=_as_http_url(raw.get("companyLogo")),
    )


def _normalize_organizer(settings: Settings, raw: dict[str, Any]) -> NavetOrganizer | None:
    organizer_id = _as_str(raw.get("id")) or _as_str(raw.get("_id"))
    name = _as_str(raw.get("name"))
    if not organizer_id or not name:
        return None
    email = _as_str(raw.get("email")) or None
    return NavetOrganizer(
        id=organizer_id,
        name=name,
        role=_as_str(raw.get("role")) or "medhjelper",
        image_url=_as_http_url(raw.get("imageUrl")),
        email=email if settings.include_organizer_emails else None,
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


async def fetch_companies(settings: Settings, client: ConvexClient) -> list[NavetCompany]:
    """Fetch the company register. Logos are resolved separately; this call omits them."""
    value = await client.query(_Q_COMPANIES, {})
    if not isinstance(value, list):
        raise UpstreamError("companies query returned a non-list payload")

    companies = [_normalize_company(raw) for raw in value if isinstance(raw, dict)]
    return sorted((c for c in companies if c is not None), key=lambda c: c.name.casefold())


async def fetch_job_listings(settings: Settings, client: ConvexClient) -> list[NavetJobListing]:
    """Fetch published job listings whose deadline is inside the retention window.

    Expired listings are kept for `JOBS_PAST_DAYS` for the same reason events
    are: a feed that empties out makes subscribers archive what they imported.
    """
    value = await client.query(_Q_JOBS, {})
    if not isinstance(value, list):
        raise UpstreamError("job listings query returned a non-list payload")

    cutoff = datetime.now(tz=UTC) - timedelta(days=settings.jobs_past_days)
    by_uid: dict[str, NavetJobListing] = {}

    for raw in value:
        if not isinstance(raw, dict) or raw.get("published") is not True:
            continue
        listing = _normalize_job(settings, raw)
        if listing is None or listing.deadline < cutoff:
            continue
        by_uid[listing.uid] = listing

    listings = sorted(by_uid.values(), key=lambda listing: (listing.deadline, listing.uid))
    if len(listings) > settings.max_jobs:
        log.warning("truncating job listings from %d to %d", len(listings), settings.max_jobs)
        listings = listings[-settings.max_jobs :]
    return listings


async def _resolve_logo_urls(
    settings: Settings,
    client: ConvexClient,
    companies: list[NavetCompany],
    caches: UpstreamCaches,
) -> dict[str, str]:
    """Company id -> logo URL, resolving only logos we have not seen before.

    `companies/queries:getAll` does not resolve storage URLs, so each unseen
    logo costs one `getById`. Caching on the logo id rather than the company id
    means a company that swaps its logo is re-resolved, while a steady state
    costs nothing.
    """
    pending = [c for c in companies if c.logo_id and c.logo_id not in caches.logo_url_by_logo_id]

    if pending:
        log.info("resolving %d unseen company logo(s)", len(pending))
        results = await _gather_limited(settings, [client.query(_Q_COMPANY, {"id": c.id}) for c in pending])
        for company, result in zip(pending, results, strict=True):
            if isinstance(result, BaseException):
                # A missing logo must never fail a refresh; leave it uncached so
                # the next refresh tries again rather than caching the failure.
                log.warning("could not resolve logo for %s: %s", company.name, result)
                continue
            url = _as_http_url(result.get("imageUrl")) if isinstance(result, dict) else None
            caches.logo_url_by_logo_id[company.logo_id or ""] = url

    resolved: dict[str, str] = {}
    for company in companies:
        url = caches.logo_url_by_logo_id.get(company.logo_id or "") if company.logo_id else None
        if url:
            resolved[company.id] = url
    return resolved


async def _resolve_media_types(
    settings: Settings,
    client: ConvexClient,
    urls: set[str],
    caches: UpstreamCaches,
) -> dict[str, str]:
    """URL -> media type, for the URLs whose type we do not already know.

    RFC 7986 wants a FMTTYPE on IMAGE, and we need the type anyway to skip the
    formats consumers cannot render. Convex storage URLs are per uploaded file,
    so caching on the URL never goes stale.
    """
    pending = sorted(url for url in urls if url not in caches.media_type_by_url)
    if pending:
        log.info("probing %d unseen asset(s) for media type", len(pending))
        results = await _gather_limited(settings, [client.media_type(url) for url in pending])
        for url, result in zip(pending, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("could not probe %s: %s", url, result)
                continue
            if result is None:
                # `media_type` answers None for a failed probe as well as for a
                # response with no usable Content-Type. Caching that would turn
                # one bad minute into a permanently image-less company, so leave
                # it out and let the next refresh ask again.
                continue
            caches.media_type_by_url[url] = result

    return {url: media for url in urls if (media := caches.media_type_by_url.get(url))}


async def _fetch_organizers(
    settings: Settings,
    client: ConvexClient,
    events: list[NavetEvent],
) -> dict[str, tuple[NavetOrganizer, ...]]:
    """Event uid -> organizers. One query per event; upstream has no bulk form.

    Upcoming events are enriched first so that hitting the lookup ceiling costs
    history rather than the events anyone is actually about to attend.
    """
    now = datetime.now(tz=UTC)
    ordered = sorted(events, key=lambda e: (e.start < now, abs((e.start - now).total_seconds())))
    targets = ordered[: settings.max_organizer_lookups]
    if len(ordered) > len(targets):
        log.warning(
            "organizer lookups capped at %d; %d event(s) will have none",
            settings.max_organizer_lookups,
            len(ordered) - len(targets),
        )
    if not targets:
        return {}

    results = await _gather_limited(settings, [client.query(_Q_EVENT, {"identifier": e.uid}) for e in targets])

    by_uid: dict[str, tuple[NavetOrganizer, ...]] = {}
    failures = 0
    for event, result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            failures += 1
            continue
        raw_organizers = result.get("organizers") if isinstance(result, dict) else None
        if not isinstance(raw_organizers, list):
            continue
        organizers = [_normalize_organizer(settings, raw) for raw in raw_organizers if isinstance(raw, dict)]
        by_uid[event.uid] = tuple(o for o in organizers if o is not None)

    if failures:
        # Organizers are decoration on top of a feed that is already correct
        # without them, so a partial failure is logged rather than raised.
        log.warning("organizer lookup failed for %d of %d event(s)", failures, len(targets))
    return by_uid


async def fetch_dataset(settings: Settings, client: ConvexClient, caches: UpstreamCaches) -> Dataset:
    """Fetch everything the service exposes, in one refresh.

    Events are the only mandatory part: they are what the calendar subscribers
    depend on, so a failure there propagates. Companies, job listings, logos and
    organizers are enrichment — if any of them fails, the refresh still
    publishes, just with less detail than usual.
    """
    events = await fetch_events(settings, client)

    companies: list[NavetCompany] = []
    if settings.fetch_companies:
        try:
            companies = await fetch_companies(settings, client)
        except UpstreamError as exc:
            log.error("company register unavailable: %s", exc)

    job_listings: list[NavetJobListing] = []
    if settings.fetch_job_listings:
        try:
            job_listings = await fetch_job_listings(settings, client)
        except UpstreamError as exc:
            log.error("job listings unavailable: %s", exc)

    if settings.event_images:
        # Job listings carry an already-resolved logo URL, so seeding from them
        # first means most companies never need a getById at all.
        logo_by_company = {j.company_id: j.image_url for j in job_listings if j.company_id and j.image_url}
        logo_by_company.update(await _resolve_logo_urls(settings, client, companies, caches))

        urls = {url for url in logo_by_company.values() if url}
        media_types = await _resolve_media_types(settings, client, urls, caches)

        def asset(company_id: str) -> tuple[str | None, str | None]:
            """The logo to publish for a company, or (None, None) if unusable."""
            url = logo_by_company.get(company_id)
            if not url:
                return None, None
            media = media_types.get(url)
            # An unknown or unsupported type is dropped rather than guessed:
            # pointing a client at an SVG it cannot render is worse than
            # pointing it at nothing.
            if media not in settings.image_types:
                return None, None
            return url, media

        companies = [_with_asset(company, *asset(company.id)) for company in companies]
        events = [_with_asset(event, *asset(event.company_id)) for event in events]
        job_listings = [_with_asset(job, *asset(job.company_id)) for job in job_listings]
    else:
        # Job listings are the one record type that arrives with a logo URL
        # already resolved, so they need clearing explicitly. Leaving it would
        # publish an image nothing had checked the format of, from the code path
        # whose whole job is to publish no images.
        job_listings = [_with_asset(job, None, None) for job in job_listings]

    if settings.fetch_organizers and events:
        organizers = await _fetch_organizers(settings, client, events)
        events = [replace(event, organizers=organizers.get(event.uid, ())) for event in events]

    log.info(
        "fetched %d event(s), %d company/companies, %d job listing(s)",
        len(events),
        len(companies),
        len(job_listings),
    )
    return Dataset(events=events, companies=companies, job_listings=job_listings)


def _with_asset(record: Any, url: str | None, media: str | None) -> Any:
    """Return `record` with its image fields set. All three types are frozen."""
    return replace(record, image_url=url, image_type=media)
