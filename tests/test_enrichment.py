"""Tests for the enrichment steps layered on top of the event fetch.

Companies, job listings, logos and organizers all cost extra upstream calls
against somebody else's Convex deployment, so the properties under test are as
much about call volume and blast radius as about the values returned: caches
must actually cache, and a failure in any of these must not take the refresh
down with it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from navet_ics.config import Settings
from navet_ics.upstream import (
    ConvexClient,
    UpstreamCaches,
    fetch_dataset,
    fetch_job_listings,
)

NOW = datetime.now(tz=UTC)
CONVEX = "https://gallant-pheasant-518.convex.cloud"


def event_payload(**overrides) -> dict:
    base = {
        "_id": "event-1",
        "_creationTime": 1736705976000.0,
        "title": "Bedriftspresentasjon med Bekk",
        "teaser": "Faglig kveld",
        "description": "<p>Velkommen</p>",
        "eventStart": NOW.timestamp() * 1000,
        "registrationOpens": (NOW - timedelta(days=7)).timestamp() * 1000,
        "participationLimit": 40.0,
        "location": "Skur 39",
        "food": "Tapas",
        "language": "Norsk",
        "ageRestriction": "Ingen",
        "hostingCompany": "company-1",
        "hostingCompanyName": "Bekk",
        "published": True,
        "slug": "h26-bekk",
        "formId": "form-1",
    }
    return {**base, **overrides}


def job_payload(**overrides) -> dict:
    base = {
        "_id": "job-1",
        "_creationTime": 1736705976000.0,
        "title": "Sommerjobb 2027",
        "type": "Sommerjobb",
        "teaser": "Bli med",
        "description": "<p>Søk her</p>",
        "applicationUrl": "https://example.com/apply",
        "deadline": (NOW + timedelta(days=20)).timestamp() * 1000,
        "company": "company-1",
        "companyName": "Bekk",
        "companyLogo": f"{CONVEX}/api/storage/logo-uuid",
        "published": True,
    }
    return {**base, **overrides}


def company_payload(**overrides) -> dict:
    base = {
        "_id": "company-1",
        "_creationTime": 1736705976000.0,
        "name": "Bekk",
        "description": "<p>Konsulentselskap</p>",
        "orgNumber": 981566378.0,
        "mainSponsor": True,
        "logo": "logo-1",
    }
    return {**base, **overrides}


class Upstream:
    """A stub Convex deployment that counts what was asked of it."""

    def __init__(self, *, events=None, companies=None, jobs=None, organizers=None, media_type="image/png"):
        self.events = events if events is not None else [event_payload()]
        self.companies = companies if companies is not None else [company_payload()]
        self.jobs = jobs if jobs is not None else [job_payload()]
        self.organizers = organizers if organizers is not None else []
        self.media_type = media_type
        self.calls: list[str] = []
        self.head_calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            self.head_calls.append(str(request.url))
            return httpx.Response(200, headers={"Content-Type": self.media_type})

        path = json.loads(request.content)["path"]
        self.calls.append(path)

        if path.endswith("getPossibleSemesters"):
            return httpx.Response(200, json={"status": "success", "value": [{"semester": "høst", "year": 2026.0}]})
        if path.endswith("events/queries:getAll"):
            return httpx.Response(200, json={"status": "success", "value": {"published": self.events}})
        if path.endswith("getEvent"):
            return httpx.Response(200, json={"status": "success", "value": {"organizers": self.organizers}})
        if path.endswith("companies/queries:getAll"):
            return httpx.Response(200, json={"status": "success", "value": self.companies})
        if path.endswith("companies/queries:getById"):
            company = {**self.companies[0], "imageUrl": f"{CONVEX}/api/storage/logo-uuid"}
            return httpx.Response(200, json={"status": "success", "value": company})
        if path.endswith("jobListings/queries:getAll"):
            return httpx.Response(200, json={"status": "success", "value": self.jobs})
        return httpx.Response(404)

    def client(self, settings: Settings) -> ConvexClient:
        client = ConvexClient(settings)
        client._client = httpx.AsyncClient(base_url=settings.convex_url, transport=httpx.MockTransport(self.handle))
        return client


def make_settings(**overrides) -> Settings:
    settings = Settings()
    for key, value in overrides.items():
        object.__setattr__(settings, key, value)
    return settings


# ---- job listings --------------------------------------------------------


async def test_unpublished_and_expired_listings_are_dropped():
    settings = make_settings(jobs_past_days=30)
    upstream = Upstream(
        jobs=[
            job_payload(),
            job_payload(_id="job-draft", published=False),
            job_payload(_id="job-ancient", deadline=(NOW - timedelta(days=400)).timestamp() * 1000),
        ]
    )
    client = upstream.client(settings)

    listings = await fetch_job_listings(settings, client)

    assert [listing.uid for listing in listings] == ["job-1"]
    await client.aclose()


async def test_recently_expired_listings_are_retained():
    """The feed must not empty out, for the same reason the events feed must not."""
    settings = make_settings(jobs_past_days=30)
    upstream = Upstream(jobs=[job_payload(deadline=(NOW - timedelta(days=5)).timestamp() * 1000)])
    client = upstream.client(settings)

    assert len(await fetch_job_listings(settings, client)) == 1
    await client.aclose()


async def test_listing_without_a_deadline_is_skipped():
    settings = make_settings()
    upstream = Upstream(jobs=[job_payload(deadline=None)])
    client = upstream.client(settings)

    assert await fetch_job_listings(settings, client) == []
    await client.aclose()


# ---- logos ---------------------------------------------------------------


async def test_logo_and_media_type_are_resolved_once_and_then_cached():
    settings = make_settings(fetch_organizers=False)
    upstream = Upstream()
    client = upstream.client(settings)
    caches = UpstreamCaches()

    first = await fetch_dataset(settings, client, caches)
    lookups_after_first = upstream.calls.count("companies/queries:getById")
    heads_after_first = len(upstream.head_calls)

    second = await fetch_dataset(settings, client, caches)

    assert first.events[0].image_type == "image/png"
    assert second.events[0].image_url == first.events[0].image_url
    # The second refresh must cost nothing extra: logos are cached on the logo
    # id and media types on the URL, both of which change when the file does.
    assert upstream.calls.count("companies/queries:getById") == lookups_after_first
    assert len(upstream.head_calls) == heads_after_first
    await client.aclose()


async def test_unsupported_media_type_is_dropped_rather_than_published():
    """Pointing a client at an SVG it cannot render is worse than no image."""
    settings = make_settings(fetch_organizers=False)
    upstream = Upstream(media_type="image/svg+xml")
    client = upstream.client(settings)

    dataset = await fetch_dataset(settings, client, UpstreamCaches())

    assert dataset.events[0].image_url is None
    assert dataset.companies[0].image_url is None
    await client.aclose()


async def test_off_host_asset_urls_are_refused():
    """The URL comes from upstream data, so it must not be able to redirect us out."""
    settings = make_settings()
    upstream = Upstream()
    client = upstream.client(settings)

    assert await client.media_type("https://evil.example.com/logo.png") is None
    assert upstream.head_calls == []
    await client.aclose()


async def test_images_can_be_switched_off_entirely():
    settings = make_settings(event_images=False, fetch_organizers=False)
    upstream = Upstream()
    client = upstream.client(settings)

    dataset = await fetch_dataset(settings, client, UpstreamCaches())

    assert dataset.events[0].image_url is None
    assert dataset.companies[0].image_url is None
    # Job listings are the trap here: upstream hands them a resolved logo URL,
    # so "do not publish images" has to actively clear it rather than merely
    # skip the resolution step.
    assert dataset.job_listings[0].image_url is None
    assert dataset.job_listings[0].image_type is None
    assert upstream.head_calls == []
    assert "companies/queries:getById" not in upstream.calls
    await client.aclose()


async def test_job_logo_is_still_format_checked_when_images_are_on():
    """The free URL on a job listing must not bypass the IMAGE_TYPES filter."""
    settings = make_settings(event_images=True, fetch_organizers=False)
    upstream = Upstream(media_type="image/svg+xml")
    client = upstream.client(settings)

    dataset = await fetch_dataset(settings, client, UpstreamCaches())

    assert dataset.job_listings[0].image_url is None
    await client.aclose()


async def test_a_failed_media_probe_is_retried_rather_than_cached():
    """Caching a failure would turn one bad minute into a permanent regression."""
    settings = make_settings(fetch_organizers=False)
    upstream = Upstream()
    caches = UpstreamCaches()

    failing = True

    def sometimes(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD" and failing:
            upstream.head_calls.append(str(request.url))
            return httpx.Response(500)
        return upstream.handle(request)

    client = ConvexClient(settings)
    client._client = httpx.AsyncClient(base_url=CONVEX, transport=httpx.MockTransport(sometimes))

    first = await fetch_dataset(settings, client, caches)
    assert first.events[0].image_url is None
    assert caches.media_type_by_url == {}

    failing = False
    second = await fetch_dataset(settings, client, caches)

    assert second.events[0].image_type == "image/png"
    await client.aclose()


# ---- organizers ----------------------------------------------------------

ORGANIZER = {
    "id": "org-1",
    "name": "Anders Rød",
    "role": "hovedansvarlig",
    "imageUrl": "https://img.clerk.com/x",
    "email": "anders@example.com",
}


async def test_organizer_emails_are_withheld_by_default():
    settings = make_settings(event_images=False)
    upstream = Upstream(organizers=[ORGANIZER])
    client = upstream.client(settings)

    dataset = await fetch_dataset(settings, client, UpstreamCaches())
    organizer = dataset.events[0].organizers[0]

    assert organizer.name == "Anders Rød"
    assert organizer.role == "hovedansvarlig"
    assert organizer.email is None
    await client.aclose()


async def test_organizer_emails_are_included_when_explicitly_enabled():
    settings = make_settings(event_images=False, include_organizer_emails=True)
    upstream = Upstream(organizers=[ORGANIZER])
    client = upstream.client(settings)

    dataset = await fetch_dataset(settings, client, UpstreamCaches())

    assert dataset.events[0].organizers[0].email == "anders@example.com"
    await client.aclose()


async def test_organizer_lookups_are_capped():
    settings = make_settings(event_images=False, max_organizer_lookups=1)
    upstream = Upstream(
        events=[event_payload(_id=f"event-{i}", slug=f"slug-{i}") for i in range(5)],
        organizers=[ORGANIZER],
    )
    client = upstream.client(settings)

    dataset = await fetch_dataset(settings, client, UpstreamCaches())

    assert upstream.calls.count("events/queries:getEvent") == 1
    assert sum(1 for event in dataset.events if event.organizers) == 1
    await client.aclose()


async def test_organizers_can_be_switched_off():
    settings = make_settings(event_images=False, fetch_organizers=False)
    upstream = Upstream(organizers=[ORGANIZER])
    client = upstream.client(settings)

    dataset = await fetch_dataset(settings, client, UpstreamCaches())

    assert dataset.events[0].organizers == ()
    assert "events/queries:getEvent" not in upstream.calls
    await client.aclose()


# ---- blast radius --------------------------------------------------------


async def test_enrichment_failures_do_not_fail_the_refresh():
    """Events are the product; everything else is decoration that may go missing."""
    upstream = Upstream()

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(500)
        path = json.loads(request.content)["path"]
        if path.startswith(("companies/", "jobListings/")) or path.endswith("getEvent"):
            return httpx.Response(500)
        return upstream.handle(request)

    settings_fast = make_settings(http_retries=0)
    client = ConvexClient(settings_fast)
    client._client = httpx.AsyncClient(base_url=CONVEX, transport=httpx.MockTransport(flaky))

    dataset = await fetch_dataset(settings_fast, client, UpstreamCaches())

    assert len(dataset.events) == 1
    assert dataset.companies == []
    assert dataset.job_listings == []
    assert dataset.events[0].organizers == ()
    await client.aclose()


async def test_registration_time_and_form_flag_survive_normalization():
    settings = make_settings(event_images=False, fetch_organizers=False)
    upstream = Upstream()
    client = upstream.client(settings)

    dataset = await fetch_dataset(settings, client, UpstreamCaches())
    event = dataset.events[0]

    assert event.registration_opens is not None
    assert event.has_registration_form is True
    assert event.company_id == "company-1"
    await client.aclose()
