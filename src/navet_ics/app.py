"""HTTP surface: the calendar feed plus health and readiness probes."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import format_datetime

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from .config import get_settings
from .store import FeedStore

log = logging.getLogger(__name__)

CALENDAR_CONTENT_TYPE = "text/calendar; charset=utf-8"


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
    await store.start()
    try:
        yield
    finally:
        await store.stop()


app = FastAPI(
    title="Navet calendar proxy",
    description="Exposes ifinavet.no events as a subscribable iCalendar feed.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/calendar.ics", status_code=302)


@app.get("/calendar.ics")
@app.head("/calendar.ics")
async def calendar(request: Request) -> Response:
    store: FeedStore = request.app.state.store
    settings = request.app.state.settings
    snapshot = store.snapshot

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
        "Content-Disposition": 'inline; filename="navet.ics"',
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


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """Liveness: the process is up and serving."""
    return JSONResponse({"status": "ok"})


@app.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    """Readiness: a feed exists and upstream data is not dangerously old."""
    store: FeedStore = request.app.state.store
    snapshot = store.snapshot

    payload = {
        "ready": snapshot is not None and not store.is_stale,
        "events": snapshot.event_count if snapshot else 0,
        "generated_at": snapshot.generated_at.isoformat() if snapshot else None,
        "last_success": store.last_success.isoformat() if store.last_success else None,
        "last_attempt": store.last_attempt.isoformat() if store.last_attempt else None,
        "last_error": store.last_error,
        "stale": store.is_stale,
        "now": datetime.now(tz=UTC).isoformat(),
    }
    return JSONResponse(payload, status_code=200 if payload["ready"] else 503)
