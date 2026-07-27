"""Tests for fetching and normalizing upstream data.

The upstream is a public Convex deployment, so these use a stub transport rather
than the network: CI must not depend on ifinavet.no being reachable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from navet_ics.config import Settings
from navet_ics.upstream import ConvexClient, UpstreamError, fetch_events

NOW = datetime.now(tz=UTC)


def event_payload(**overrides) -> dict:
    base = {
        "_id": "j97c3ym0f8t8dq611a822g18js7nejwa",
        "_creationTime": 1736705976000.0,
        "title": "Bedriftspresentasjon med DNV",
        "teaser": "Faglig kveld",
        "description": "<p>Velkommen</p>",
        "eventStart": NOW.timestamp() * 1000,
        "registrationOpens": NOW.timestamp() * 1000,
        "participationLimit": 40.0,
        "location": "Veritasveien 1",
        "food": "Pizza",
        "language": "Norsk",
        "ageRestriction": "Ingen",
        "hostingCompany": "k97asv3rjzdp52twndzrw41c1d7ncnsf",
        "hostingCompanyName": "DNV",
        "published": True,
        "slug": "v26-dnv",
    }
    return {**base, **overrides}


def build_client(handler) -> ConvexClient:
    """A ConvexClient whose transport is a stub, so no network is touched."""
    settings = Settings()
    client = ConvexClient(settings)
    client._client = httpx.AsyncClient(
        base_url=settings.convex_url,
        transport=httpx.MockTransport(handler),
    )
    return client


def responder(*, semesters=None, published=None, unpublished=None):
    semesters = semesters if semesters is not None else [{"semester": "vår", "year": 2026.0}]

    def handle(request: httpx.Request) -> httpx.Response:
        import json

        path = json.loads(request.content)["path"]
        if path.endswith("getPossibleSemesters"):
            return httpx.Response(200, json={"status": "success", "value": semesters})
        return httpx.Response(
            200,
            json={
                "status": "success",
                "value": {"published": published or [], "unpublished": unpublished or []},
            },
        )

    return handle


async def test_normalizes_a_published_event():
    settings = Settings()
    client = build_client(responder(published=[event_payload()]))

    events = await fetch_events(settings, client)

    assert len(events) == 1
    event = events[0]
    assert event.uid == "j97c3ym0f8t8dq611a822g18js7nejwa"
    assert event.company == "DNV"
    assert event.url == "https://ifinavet.no/events/v26-dnv"
    assert event.start.tzinfo is not None
    await client.aclose()


async def test_falls_back_to_id_when_slug_is_absent():
    settings = Settings()
    client = build_client(responder(published=[event_payload(slug=None)]))

    events = await fetch_events(settings, client)

    # The site routes /events/<identifier> by slug or id, so the id still works.
    assert events[0].url.endswith("/events/j97c3ym0f8t8dq611a822g18js7nejwa")
    await client.aclose()


async def test_skips_events_without_a_usable_start_or_id():
    settings = Settings()
    client = build_client(
        responder(published=[event_payload(eventStart=None), event_payload(_id="", slug="x"), event_payload()])
    )

    events = await fetch_events(settings, client)

    assert len(events) == 1
    await client.aclose()


async def test_drops_events_older_than_the_history_window():
    settings = Settings()
    old = (NOW - timedelta(days=settings.past_days + 30)).timestamp() * 1000
    client = build_client(responder(published=[event_payload(_id="old", eventStart=old), event_payload()]))

    events = await fetch_events(settings, client)

    assert [e.uid for e in events] == ["j97c3ym0f8t8dq611a822g18js7nejwa"]
    await client.aclose()


async def test_ignores_records_not_marked_published():
    settings = Settings()
    client = build_client(responder(published=[event_payload(published=False)]))

    assert await fetch_events(settings, client) == []
    await client.aclose()


async def test_deduplicates_events_seen_in_multiple_semesters():
    settings = Settings()
    client = build_client(
        responder(
            semesters=[{"semester": "vår", "year": 2026.0}, {"semester": "høst", "year": 2026.0}],
            published=[event_payload()],
        )
    )

    events = await fetch_events(settings, client)

    assert len(events) == 1
    await client.aclose()


async def test_events_are_sorted_by_start_time():
    settings = Settings()
    later = (NOW + timedelta(days=10)).timestamp() * 1000
    client = build_client(responder(published=[event_payload(_id="b", eventStart=later), event_payload(_id="a")]))

    events = await fetch_events(settings, client)

    assert [e.uid for e in events] == ["a", "b"]
    await client.aclose()


async def test_function_level_error_is_not_retried_and_propagates():
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status": "error", "errorMessage": "boom"})

    settings = Settings()
    client = build_client(handle)

    with pytest.raises(UpstreamError):
        await fetch_events(settings, client)

    # Retrying a function-level error just burns time; it will fail identically.
    assert calls == 1
    await client.aclose()


async def test_client_error_is_not_retried():
    """A 4xx will repeat identically, so retrying only delays the real error."""
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="not found")

    settings = Settings()
    object.__setattr__(settings, "http_backoff", 0.01)
    client = ConvexClient(settings)
    client._client = httpx.AsyncClient(base_url=settings.convex_url, transport=httpx.MockTransport(handle))

    with pytest.raises(UpstreamError, match="404"):
        await fetch_events(settings, client)

    assert calls == 1
    await client.aclose()


async def test_rate_limit_is_retried():
    """429 is transient, unlike the other 4xx codes."""
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="slow down")
        return responder(published=[event_payload()])(request)

    settings = Settings()
    object.__setattr__(settings, "http_backoff", 0.01)
    client = ConvexClient(settings)
    client._client = httpx.AsyncClient(base_url=settings.convex_url, transport=httpx.MockTransport(handle))

    assert len(await fetch_events(settings, client)) == 1
    await client.aclose()


async def test_one_failing_semester_does_not_lose_the_others():
    """A single broken semester query must not block every other semester."""

    def handle(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        if body["path"].endswith("getPossibleSemesters"):
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "value": [{"semester": "vår", "year": 2026.0}, {"semester": "høst", "year": 2026.0}],
                },
            )
        if body["args"]["semester"] == "vår":
            return httpx.Response(400, text="broken")
        return responder(published=[event_payload()])(request)

    settings = Settings()
    object.__setattr__(settings, "http_backoff", 0.01)
    client = ConvexClient(settings)
    client._client = httpx.AsyncClient(base_url=settings.convex_url, transport=httpx.MockTransport(handle))

    events = await fetch_events(settings, client)

    assert len(events) == 1
    await client.aclose()


async def test_every_semester_failing_raises_rather_than_returning_empty():
    """An empty feed would archive real events downstream, so this must raise."""

    def handle(request: httpx.Request) -> httpx.Response:
        import json

        if json.loads(request.content)["path"].endswith("getPossibleSemesters"):
            return httpx.Response(200, json={"status": "success", "value": [{"semester": "vår", "year": 2026.0}]})
        return httpx.Response(400, text="broken")

    settings = Settings()
    object.__setattr__(settings, "http_backoff", 0.01)
    client = ConvexClient(settings)
    client._client = httpx.AsyncClient(base_url=settings.convex_url, transport=httpx.MockTransport(handle))

    with pytest.raises(UpstreamError):
        await fetch_events(settings, client)

    await client.aclose()


async def test_server_error_is_retried_then_raises():
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="unavailable")

    settings = Settings()
    object.__setattr__(settings, "http_backoff", 0.01)
    client = ConvexClient(settings)
    client._client = httpx.AsyncClient(base_url=settings.convex_url, transport=httpx.MockTransport(handle))

    with pytest.raises(UpstreamError):
        await fetch_events(settings, client)

    assert calls == settings.http_retries + 1
    await client.aclose()


async def test_transient_failure_recovers_on_retry():
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text="oops")
        return responder(published=[event_payload()])(request)

    settings = Settings()
    object.__setattr__(settings, "http_backoff", 0.01)
    client = ConvexClient(settings)
    client._client = httpx.AsyncClient(base_url=settings.convex_url, transport=httpx.MockTransport(handle))

    events = await fetch_events(settings, client)

    assert len(events) == 1
    await client.aclose()
