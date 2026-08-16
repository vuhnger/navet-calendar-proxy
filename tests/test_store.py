"""Tests for the refresh/cache layer.

The behaviour under test is what protects subscribers: an upstream that is
reachable but wrong must not be allowed to replace a good feed, because a
subscriber that stops seeing a UID archives that event.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from navet_ics.config import Settings
from navet_ics.store import FeedStore, _etag_for
from navet_ics.upstream import Dataset, NavetEvent, NavetJobListing, UpstreamError


def make_events(count: int) -> list[NavetEvent]:
    return [
        NavetEvent(
            uid=f"event-{index}",
            title=f"Arrangement {index}",
            start=datetime(2026, 2, 10, 15, 0, tzinfo=UTC),
            teaser="",
            description_html="",
            location="Ole-Johan Dahls hus",
            company="Navet",
            food="",
            language="Norsk",
            age_restriction="",
            url=f"https://ifinavet.no/events/event-{index}",
            external_url=None,
            created=datetime(2026, 1, 1, tzinfo=UTC),
            participation_limit=None,
        )
        for index in range(count)
    ]


@pytest.fixture
def store(tmp_path, monkeypatch) -> FeedStore:
    settings = Settings()
    object.__setattr__(settings, "state_dir", str(tmp_path))
    return FeedStore(settings)


def make_jobs(count: int) -> list[NavetJobListing]:
    return [
        NavetJobListing(
            uid=f"job-{index}",
            title=f"Sommerjobb {index}",
            kind="Sommerjobb",
            teaser="",
            description_html="",
            application_url=None,
            deadline=datetime(2026, 3, 1, 23, 59, tzinfo=UTC),
            company_id="company-1",
            company="Bekk",
            url=f"https://ifinavet.no/job/job-{index}",
            created=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for index in range(count)
    ]


def stub_fetch(
    store: FeedStore,
    monkeypatch,
    events: list[NavetEvent],
    job_listings: list[NavetJobListing] | None = None,
) -> None:
    async def fake_fetch(settings, client, caches):
        return Dataset(events=events, job_listings=job_listings or [])

    monkeypatch.setattr("navet_ics.store.fetch_dataset", fake_fetch)


async def test_refresh_publishes_events(store, monkeypatch):
    stub_fetch(store, monkeypatch, make_events(10))

    snapshot = await store.refresh()

    assert snapshot.event_count == 10
    assert store.last_success is not None
    assert store.last_error is None
    await store.stop()


async def test_empty_successful_response_does_not_replace_a_good_feed(store, monkeypatch):
    """The dangerous case: upstream says success but returns nothing."""
    stub_fetch(store, monkeypatch, make_events(10))
    good = await store.refresh()

    stub_fetch(store, monkeypatch, [])
    with pytest.raises(UpstreamError):
        await store.refresh()

    # The previously good feed is still what subscribers get.
    assert store.snapshot is good
    assert store.snapshot.event_count == 10
    assert b"BEGIN:VEVENT" in store.snapshot.body
    await store.stop()


async def test_large_partial_drop_is_rejected(store, monkeypatch):
    stub_fetch(store, monkeypatch, make_events(20))
    await store.refresh()

    # Losing 80% of the feed is upstream breakage, not 16 cancellations.
    stub_fetch(store, monkeypatch, make_events(4))
    with pytest.raises(UpstreamError):
        await store.refresh()

    assert store.snapshot.event_count == 20
    await store.stop()


async def test_plausible_shrinkage_is_allowed(store, monkeypatch):
    stub_fetch(store, monkeypatch, make_events(10))
    await store.refresh()

    # Events genuinely do age out of the window; this must still go through.
    stub_fetch(store, monkeypatch, make_events(8))
    snapshot = await store.refresh()

    assert snapshot.event_count == 8
    await store.stop()


async def test_growth_is_always_allowed(store, monkeypatch):
    stub_fetch(store, monkeypatch, make_events(2))
    await store.refresh()

    stub_fetch(store, monkeypatch, make_events(40))
    assert (await store.refresh()).event_count == 40
    await store.stop()


async def test_first_ever_refresh_may_be_empty(store, monkeypatch):
    """With no previous feed there is nothing to protect, so an empty result is
    a legitimate answer (Navet really may have nothing scheduled)."""
    stub_fetch(store, monkeypatch, [])

    assert (await store.refresh()).event_count == 0
    await store.stop()


async def test_refresh_loop_survives_upstream_failure(store, monkeypatch):
    stub_fetch(store, monkeypatch, make_events(5))
    await store.refresh()

    async def boom(settings, client, caches):
        raise UpstreamError("convex is down")

    monkeypatch.setattr("navet_ics.store.fetch_dataset", boom)
    with pytest.raises(UpstreamError):
        await store.refresh()

    # Still serving, and the failure is visible to operators.
    assert store.snapshot.event_count == 5
    await store.stop()


async def test_feed_is_persisted_and_reloaded_after_restart(tmp_path, monkeypatch):
    settings = Settings()
    object.__setattr__(settings, "state_dir", str(tmp_path))

    first = FeedStore(settings)
    stub_fetch(first, monkeypatch, make_events(7))
    await first.refresh()
    await first.stop()

    # A fresh process must serve the cached feed immediately, without waiting for
    # its first upstream fetch, and must report itself ready while doing so.
    second = FeedStore(settings)
    second._load_from_disk()

    assert second.snapshot is not None
    assert second.snapshot.event_count == 7
    assert second.is_stale is False
    await second.stop()


async def test_corrupt_cache_file_is_ignored(tmp_path):
    settings = Settings()
    object.__setattr__(settings, "state_dir", str(tmp_path))
    (tmp_path / "calendar.ics").write_bytes(b"not a calendar at all")

    store = FeedStore(settings)
    store._load_from_disk()

    assert store.snapshot is None
    await store.stop()


async def test_refresh_announces_a_new_listing_exactly_once(store, monkeypatch):
    """End-to-end: the first refresh seeds, a later new listing posts, then stops."""
    import httpx

    posted: list[dict] = []

    def webhook(request: httpx.Request) -> httpx.Response:
        import json as _json

        posted.append(_json.loads(request.content))
        return httpx.Response(200)

    object.__setattr__(store._settings, "notify_webhook_url", "https://hooks.slack.com/services/T/B/x")
    store._notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(webhook))

    stub_fetch(store, monkeypatch, make_events(3), make_jobs(1))
    await store.refresh()
    assert posted == [], "the first refresh must seed silently, not announce the backlog"

    stub_fetch(store, monkeypatch, make_events(3), make_jobs(2))
    await store.refresh()
    assert len(posted) == 1
    assert "Ny stillingsannonse fra Bekk" in posted[0]["text"]

    # A refresh that turns up nothing new must stay quiet.
    await store.refresh()
    assert len(posted) == 1
    await store.stop()


async def test_a_broken_webhook_does_not_fail_the_refresh(store, monkeypatch):
    import httpx

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    object.__setattr__(store._settings, "notify_webhook_url", "https://hooks.slack.com/services/T/B/x")
    store._notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(boom))

    stub_fetch(store, monkeypatch, make_events(3), make_jobs(1))
    await store.refresh()
    stub_fetch(store, monkeypatch, make_events(3), make_jobs(2))

    snapshot = await store.refresh()

    assert snapshot.event_count == 3
    assert store.last_error is None
    await store.stop()


async def test_jobs_atom_is_rebuilt_on_every_refresh(store, monkeypatch):
    stub_fetch(store, monkeypatch, make_events(2), make_jobs(1))
    await store.refresh()
    first = store.jobs_atom

    stub_fetch(store, monkeypatch, make_events(2), make_jobs(3))
    await store.refresh()

    assert first is not None
    assert store.jobs_atom != first
    assert store.jobs_atom.count(b"<entry") == 3
    await store.stop()


async def test_etag_is_derived_from_the_served_body(store, monkeypatch):
    stub_fetch(store, monkeypatch, make_events(3))
    first = await store.refresh()

    stub_fetch(store, monkeypatch, make_events(4))
    second = await store.refresh()

    # Conditional requests are only correct if the tag tracks the exact bytes.
    assert first.etag != second.etag
    assert second.etag == _etag_for(second.body)
    await store.stop()
