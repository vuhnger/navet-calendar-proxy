"""Tests for the JSON API and the OpenAPI document.

These drive the app through a real client but with the background refresh
replaced, so nothing here touches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from navet_ics import app as app_module
from navet_ics.config import Settings
from navet_ics.store import FeedStore
from navet_ics.upstream import Dataset, NavetCompany, NavetEvent, NavetJobListing

NOW = datetime.now(tz=UTC)


def make_dataset() -> Dataset:
    events = [
        NavetEvent(
            uid="event-past",
            title="Bedriftspresentasjon med DNV",
            start=NOW - timedelta(days=10),
            teaser="Faglig kveld",
            description_html="<p>Hei &amp; velkommen</p>",
            location="Veritasveien 1",
            company="DNV",
            food="Pizza",
            language="Norsk",
            age_restriction="Ingen",
            url="https://ifinavet.no/events/v26-dnv",
            external_url=None,
            created=NOW - timedelta(days=30),
            participation_limit=40,
            company_id="company-dnv",
            registration_opens=NOW - timedelta(days=17),
            slug="v26-dnv",
        ),
        NavetEvent(
            uid="event-future",
            title="Bedriftspresentasjon med Bekk",
            start=NOW + timedelta(days=10),
            teaser="Bli kjent med oss",
            description_html="<p>Velkommen</p>",
            location="Skur 39",
            company="Bekk",
            food="Tapas",
            language="Norsk",
            age_restriction="18",
            url="https://ifinavet.no/events/h26-bekk",
            external_url=None,
            created=NOW - timedelta(days=5),
            participation_limit=None,
            company_id="company-bekk",
            registration_opens=NOW + timedelta(days=3),
            slug="h26-bekk",
        ),
    ]
    companies = [
        NavetCompany(
            id="company-bekk",
            name="Bekk",
            description_html="<p>Konsulentselskap</p>",
            org_number=981566378,
            main_sponsor=True,
            logo_id="logo-1",
            image_url="https://example.com/bekk.png",
            image_type="image/png",
        ),
        NavetCompany(
            id="company-dnv",
            name="DNV",
            description_html="",
            org_number=None,
            main_sponsor=False,
            logo_id=None,
            image_url=None,
            image_type=None,
        ),
    ]
    job_listings = [
        NavetJobListing(
            uid="job-expired",
            title="Vinterjobb",
            kind="Deltid",
            teaser="",
            description_html="",
            application_url=None,
            deadline=NOW - timedelta(days=2),
            company_id="company-dnv",
            company="DNV",
            url="https://ifinavet.no/job/job-expired",
            created=NOW - timedelta(days=60),
        ),
        NavetJobListing(
            uid="job-active",
            title="Sommerjobb 2027",
            kind="Sommerjobb",
            teaser="Bli med",
            description_html="<p>Søk her</p>",
            application_url="https://example.com/apply",
            deadline=NOW + timedelta(days=20),
            company_id="company-bekk",
            company="Bekk",
            url="https://ifinavet.no/job/job-active",
            created=NOW - timedelta(days=10),
        ),
    ]
    return Dataset(events=events, companies=companies, job_listings=job_listings)


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings()
    object.__setattr__(settings, "state_dir", str(tmp_path))
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)

    async def fake_fetch(settings, client, caches):
        return make_dataset()

    monkeypatch.setattr("navet_ics.store.fetch_dataset", fake_fetch)

    async def start(self) -> None:
        # Refresh once, synchronously, instead of starting the background loop:
        # a test must not race a task it cannot see.
        await self.refresh()

    monkeypatch.setattr(FeedStore, "start", start)

    with TestClient(app_module.app) as test_client:
        yield test_client


# ---- documentation -------------------------------------------------------


def test_openapi_document_is_served_and_describes_every_endpoint(client):
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    for path in (
        "/calendar.ics",
        "/registrations.ics",
        "/jobs.ics",
        "/api/events",
        "/api/events/{event_id}",
        "/api/companies",
        "/api/companies/{company_id}",
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/status",
    ):
        assert path in paths, f"{path} missing from the OpenAPI document"


def test_docs_pages_render(client):
    for path in ("/docs", "/redoc"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")


def test_calendar_feeds_are_documented_as_calendar_not_json(client):
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/calendar.ics"]["get"]["responses"]

    assert "text/calendar" in responses["200"]["content"]


# ---- feeds ---------------------------------------------------------------


@pytest.mark.parametrize("path", ["/calendar.ics", "/registrations.ics", "/jobs.ics"])
def test_every_feed_serves_a_calendar(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/calendar; charset=utf-8"
    assert response.content.startswith(b"BEGIN:VCALENDAR")


@pytest.mark.parametrize("path", ["/calendar.ics", "/registrations.ics", "/jobs.ics"])
def test_every_feed_honours_if_none_match(client, path):
    etag = client.get(path).headers["etag"]
    response = client.get(path, headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert response.content == b""


def test_feeds_have_distinct_bodies(client):
    bodies = {client.get(path).content for path in ("/calendar.ics", "/registrations.ics", "/jobs.ics")}

    assert len(bodies) == 3


# ---- events --------------------------------------------------------------


def test_list_events_returns_everything_by_default(client):
    payload = client.get("/api/events").json()

    assert payload["total"] == 2
    assert {item["id"] for item in payload["items"]} == {"event-past", "event-future"}


def test_upcoming_filters_out_the_past(client):
    payload = client.get("/api/events", params={"upcoming": True}).json()

    assert [item["id"] for item in payload["items"]] == ["event-future"]


def test_events_can_be_filtered_by_company_and_text(client):
    assert client.get("/api/events", params={"company_id": "company-bekk"}).json()["total"] == 1
    assert client.get("/api/events", params={"q": "dnv"}).json()["total"] == 1
    assert client.get("/api/events", params={"q": "ingenting"}).json()["total"] == 0


def test_event_exposes_registration_time_and_plain_text_description(client):
    event = client.get("/api/events/event-past").json()

    assert event["registration_opens"] is not None
    # Entities are decoded, because the HTML form is offered separately.
    assert event["description"] == "Hei & velkommen"
    assert "&amp;" in event["description_html"]


def test_event_can_be_looked_up_by_slug(client):
    assert client.get("/api/events/v26-dnv").json()["id"] == "event-past"


def test_unknown_event_is_404(client):
    assert client.get("/api/events/nope").status_code == 404


# ---- paging --------------------------------------------------------------


def test_paging_reports_the_full_total(client):
    payload = client.get("/api/events", params={"limit": 1}).json()

    assert payload["total"] == 2
    assert len(payload["items"]) == 1
    assert payload["limit"] == 1


def test_offset_walks_the_collection(client):
    first = client.get("/api/events", params={"limit": 1, "offset": 0}).json()["items"][0]["id"]
    second = client.get("/api/events", params={"limit": 1, "offset": 1}).json()["items"][0]["id"]

    assert first != second


def test_limit_is_capped_rather_than_rejected(client):
    """An unbounded query is the thing to prevent; a big one is just clamped."""
    payload = client.get("/api/events", params={"limit": 100_000}).json()

    assert payload["limit"] == Settings().max_page_size


def test_negative_paging_is_rejected(client):
    assert client.get("/api/events", params={"limit": 0}).status_code == 422
    assert client.get("/api/events", params={"offset": -1}).status_code == 422


# ---- companies and jobs --------------------------------------------------


def test_companies_are_listed_with_resolved_logos(client):
    payload = client.get("/api/companies").json()
    bekk = next(item for item in payload["items"] if item["id"] == "company-bekk")

    assert bekk["main_sponsor"] is True
    assert bekk["image_type"] == "image/png"
    assert bekk["description"] == "Konsulentselskap"


def test_unknown_company_is_404(client):
    assert client.get("/api/companies/nope").status_code == 404


def test_active_jobs_exclude_passed_deadlines(client):
    assert client.get("/api/jobs").json()["total"] == 2
    payload = client.get("/api/jobs", params={"active": True}).json()

    assert [item["id"] for item in payload["items"]] == ["job-active"]


def test_jobs_filter_by_type_case_insensitively(client):
    assert client.get("/api/jobs", params={"type": "sommerjobb"}).json()["total"] == 1


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


# ---- service -------------------------------------------------------------


def test_status_counts_every_collection(client):
    payload = client.get("/api/status").json()

    assert payload["ready"] is True
    assert (payload["events"], payload["companies"], payload["job_listings"]) == (2, 2, 2)
    assert {feed["path"] for feed in payload["feeds"]} == {
        "/calendar.ics",
        "/registrations.ics",
        "/jobs.ics",
    }


def test_readyz_agrees_with_status(client):
    assert client.get("/readyz").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}


def test_root_still_points_at_the_events_feed(client):
    """Anyone who subscribed to the bare origin must not start receiving HTML."""
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/calendar.ics"
