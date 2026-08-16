"""HTTP surface: the calendar feeds, a read-only JSON API, and health probes.

Nothing here calls upstream. Every response is served from what the background
refresh in `store` already built, so a request can never be slower than memory
and can never add load to Navet's Convex deployment.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from . import __version__
from .config import Settings, get_settings
from .models import Company, CompanyPage, Event, EventPage, FeedInfo, JobListing, JobListingPage, Status
from .store import EVENTS_FEED, FEED_PATHS, JOBS_FEED, REGISTRATIONS_FEED, FeedStore

log = logging.getLogger(__name__)

CALENDAR_CONTENT_TYPE = "text/calendar; charset=utf-8"

DESCRIPTION = """
A read-only mirror of the public event data behind
[ifinavet.no](https://ifinavet.no), published as subscribable iCalendar feeds
and as JSON.

**Calendar feeds** — paste any of these into a calendar client, or into Peoply's
*ICS-URL* field:

| Feed | Contents |
| --- | --- |
| [`/calendar.ics`](/calendar.ics) | Navet's events. The feed to subscribe to if you only want one. |
| [`/registrations.ics`](/registrations.ics) | One entry per event marking when registration opens, with a reminder. |
| [`/jobs.ics`](/jobs.ics) | Application deadlines for jobs advertised through Navet. |

The registration and job feeds are separate documents on purpose: an importer
that treated them as events would double what shows up on the other side.

**JSON** — the same data, under `/api`, including fields the calendar format has
nowhere to put. All list endpoints page with `limit` and `offset`.

Data is refreshed in the background and served from cache, so responses reflect
the last successful refresh rather than live upstream state. `/api/status`
reports how old that is.
"""

TAGS = [
    {"name": "Calendar", "description": "iCalendar feeds (RFC 5545)."},
    {"name": "Events", "description": "Navet's events."},
    {"name": "Companies", "description": "The company register events and job listings refer to."},
    {"name": "Jobs", "description": "Jobs advertised through ifinavet.no."},
    {"name": "Service", "description": "Freshness and health of this service."},
]


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    settings = get_settings()
    store = FeedStore(settings)
    app.state.store = store
    app.state.settings = settings
    # start() is inside the try: it opens an httpx connection pool, so if it
    # raises, stop() must still run or the pool leaks.
    try:
        await store.start()
        yield
    finally:
        await store.stop()


app = FastAPI(
    title="Navet calendar proxy",
    summary="ifinavet.no's events as iCalendar feeds and JSON.",
    description=DESCRIPTION,
    version=__version__,
    lifespan=lifespan,
    openapi_tags=TAGS,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={"name": "vuhnger", "url": "https://github.com/vuhnger/navet-calendar-proxy"},
    license_info={"name": "Source on GitHub", "url": "https://github.com/vuhnger/navet-calendar-proxy"},
)


def _store(request: Request) -> FeedStore:
    return request.app.state.store


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# ---- calendar feeds ------------------------------------------------------


def _serve_feed(request: Request, key: str) -> Response:
    store = _store(request)
    settings = _settings(request)
    snapshot = store.feed(key)

    if snapshot is None:
        # Nothing has ever been built. Returning an empty calendar would make
        # subscribers archive every previously imported event, so fail loudly.
        return JSONResponse(
            {"detail": "Calendar not available yet, try again shortly."},
            status_code=503,
            headers={"Retry-After": "60"},
        )

    headers = {
        "Content-Type": CALENDAR_CONTENT_TYPE,
        "Content-Disposition": f'inline; filename="navet-{key}.ics"',
        "ETag": snapshot.etag,
        "Last-Modified": format_datetime(snapshot.generated_at, usegmt=True),
        "Cache-Control": f"public, max-age={settings.refresh_interval // 2}",
        "X-Event-Count": str(snapshot.event_count),
        "X-Feed-Stale": "true" if store.is_stale else "false",
    }

    if request.headers.get("if-none-match") == snapshot.etag:
        return Response(status_code=304, headers=headers)

    body = b"" if request.method == "HEAD" else snapshot.body
    return Response(content=body, status_code=200, headers=headers)


_FEED_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/calendar": {}}, "description": "The calendar document."},
    304: {"description": "Unchanged since the `If-None-Match` ETag."},
    503: {"description": "No data has been built yet."},
}


@app.get("/calendar.ics", tags=["Calendar"], responses=_FEED_RESPONSES, response_class=Response)
@app.head("/calendar.ics", include_in_schema=False)
async def calendar(request: Request) -> Response:
    """Navet's events, one VEVENT each.

    Upstream stores no end time, so every event is given
    `DTEND = DTSTART + DEFAULT_DURATION_MINUTES`. UIDs are `<event id>@ifinavet.no`
    and stable across refreshes.
    """
    return _serve_feed(request, EVENTS_FEED)


@app.get("/registrations.ics", tags=["Calendar"], responses=_FEED_RESPONSES, response_class=Response)
@app.head("/registrations.ics", include_in_schema=False)
async def registrations(request: Request) -> Response:
    """When registration opens, one VEVENT per event that has an opening time.

    Entries are TRANSPARENT (they do not make you look busy) and carry a VALARM
    `REMINDER_ALARM_MINUTES` ahead of the opening. UIDs are
    `<event id>-registration@ifinavet.no`.
    """
    return _serve_feed(request, REGISTRATIONS_FEED)


@app.get("/jobs.ics", tags=["Calendar"], responses=_FEED_RESPONSES, response_class=Response)
@app.head("/jobs.ics", include_in_schema=False)
async def jobs_feed(request: Request) -> Response:
    """Application deadlines for jobs advertised through Navet.

    UIDs are `<listing id>-deadline@ifinavet.no`. Expired listings are kept for
    `JOBS_PAST_DAYS` so the feed does not empty out.
    """
    return _serve_feed(request, JOBS_FEED)


# ---- JSON API ------------------------------------------------------------


def _document(request: Request):
    document = _store(request).document
    if document is None:
        raise HTTPException(
            status_code=503,
            detail="No data has been fetched yet, try again shortly.",
            headers={"Retry-After": "60"},
        )
    return document


def _slice(request: Request, items: list, limit: int | None, offset: int) -> dict[str, Any]:
    """The paging envelope's fields, shared by every list endpoint."""
    settings = _settings(request)
    size = min(limit or settings.default_page_size, settings.max_page_size)
    return {"total": len(items), "limit": size, "offset": offset, "items": items[offset : offset + size]}


LimitQuery = Annotated[int | None, Query(ge=1, description="Page size. Capped by `MAX_PAGE_SIZE`.")]
OffsetQuery = Annotated[int, Query(ge=0, description="Items to skip.")]

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such record in the current dataset."}}
_UNAVAILABLE: dict[int | str, dict[str, Any]] = {503: {"description": "No data has been fetched yet."}}


@app.get("/api/events", tags=["Events"], responses=_UNAVAILABLE)
async def list_events(
    request: Request,
    limit: LimitQuery = None,
    offset: OffsetQuery = 0,
    upcoming: Annotated[bool, Query(description="Only events that have not started yet.")] = False,
    company_id: Annotated[str | None, Query(description="Restrict to one hosting company.")] = None,
    q: Annotated[str | None, Query(description="Case-insensitive substring of the title or teaser.")] = None,
) -> EventPage:
    """Published events, oldest first.

    Covers `PAST_DAYS` of history plus everything upstream knows about ahead,
    which is why the default ordering starts in the past. Pass `upcoming=true`
    for the forward-looking view.
    """
    events: list[Event] = list(_document(request).events)

    if upcoming:
        now = datetime.now(tz=UTC)
        events = [e for e in events if e.start >= now]
    if company_id:
        events = [e for e in events if e.company_id == company_id]
    if q:
        needle = q.casefold()
        events = [e for e in events if needle in e.title.casefold() or needle in e.teaser.casefold()]

    return EventPage(**_slice(request, events, limit, offset))


@app.get("/api/events/{event_id}", tags=["Events"], responses={**_NOT_FOUND, **_UNAVAILABLE})
async def get_event(
    request: Request,
    event_id: Annotated[str, Path(description="Upstream event id, or the event's slug.")],
) -> Event:
    """One event, by id or slug."""
    for event in _document(request).events:
        if event.id == event_id or (event.slug and event.slug == event_id):
            return event
    raise HTTPException(status_code=404, detail="Unknown event.")


@app.get("/api/companies", tags=["Companies"], responses=_UNAVAILABLE)
async def list_companies(
    request: Request,
    limit: LimitQuery = None,
    offset: OffsetQuery = 0,
    q: Annotated[str | None, Query(description="Case-insensitive substring of the name.")] = None,
) -> CompanyPage:
    """Navet's company register, by name.

    Includes companies with no events in the current window, because the
    register is upstream's own list rather than a projection of the events.
    """
    companies: list[Company] = list(_document(request).companies)
    if q:
        needle = q.casefold()
        companies = [c for c in companies if needle in c.name.casefold()]
    return CompanyPage(**_slice(request, companies, limit, offset))


@app.get("/api/companies/{company_id}", tags=["Companies"], responses={**_NOT_FOUND, **_UNAVAILABLE})
async def get_company(
    request: Request,
    company_id: Annotated[str, Path(description="Upstream company id.")],
) -> Company:
    """One company, by id."""
    for company in _document(request).companies:
        if company.id == company_id:
            return company
    raise HTTPException(status_code=404, detail="Unknown company.")


@app.get("/api/jobs", tags=["Jobs"], responses=_UNAVAILABLE)
async def list_jobs(
    request: Request,
    limit: LimitQuery = None,
    offset: OffsetQuery = 0,
    active: Annotated[bool, Query(description="Only listings whose deadline has not passed.")] = False,
    type: Annotated[str | None, Query(description="Exact listing category, case-insensitive.")] = None,
    company_id: Annotated[str | None, Query(description="Restrict to one company.")] = None,
) -> JobListingPage:
    """Published job listings, by deadline.

    Expired listings are retained for `JOBS_PAST_DAYS`; `active=true` hides them.
    """
    listings: list[JobListing] = list(_document(request).job_listings)

    if active:
        now = datetime.now(tz=UTC)
        listings = [j for j in listings if j.deadline >= now]
    if type:
        listings = [j for j in listings if j.type.casefold() == type.casefold()]
    if company_id:
        listings = [j for j in listings if j.company_id == company_id]

    return JobListingPage(**_slice(request, listings, limit, offset))


@app.get("/api/jobs/{job_id}", tags=["Jobs"], responses={**_NOT_FOUND, **_UNAVAILABLE})
async def get_job(
    request: Request,
    job_id: Annotated[str, Path(description="Upstream job listing id.")],
) -> JobListing:
    """One job listing, by id."""
    for listing in _document(request).job_listings:
        if listing.id == job_id:
            return listing
    raise HTTPException(status_code=404, detail="Unknown job listing.")


# ---- service ------------------------------------------------------------


def _status(request: Request) -> Status:
    store = _store(request)
    document = store.document
    return Status(
        ready=store.snapshot is not None and not store.is_stale,
        stale=store.is_stale,
        events=len(document.events) if document else 0,
        companies=len(document.companies) if document else 0,
        job_listings=len(document.job_listings) if document else 0,
        generated_at=store.snapshot.generated_at if store.snapshot else None,
        last_success=store.last_success,
        last_attempt=store.last_attempt,
        last_error=store.last_error,
        feeds=[
            FeedInfo(path=FEED_PATHS[key], events=snapshot.event_count, etag=snapshot.etag)
            for key, snapshot in store.feeds.items()
        ],
        now=datetime.now(tz=UTC),
    )


@app.get("/api/status", tags=["Service"])
async def status(request: Request) -> Status:
    """How fresh the served data is, and what the last refresh did.

    Always answers `200`, including when the data is stale or missing — it is
    the endpoint for looking at the problem. Use `/readyz` for a probe that
    fails instead.
    """
    return _status(request)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    # Kept pointing at the feed rather than the docs: anyone who subscribed to
    # the bare origin in a calendar client would get HTML otherwise.
    return RedirectResponse("/calendar.ics", status_code=302)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """Liveness: the process is up and serving."""
    return JSONResponse({"status": "ok"})


@app.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    """Readiness: a feed exists and upstream data is not dangerously old."""
    payload = _status(request)
    return JSONResponse(
        payload.model_dump(mode="json"),
        status_code=200 if payload.ready else 503,
    )
