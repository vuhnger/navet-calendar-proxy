"""Tests for the Atom feed of job listings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree as ET

import pytest

from navet_ics.config import Settings
from navet_ics.syndication import ATOM_NS, build_jobs_atom
from navet_ics.upstream import NavetJobListing

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
NS = {"a": ATOM_NS}


@pytest.fixture
def settings() -> Settings:
    return Settings()


def make_job(uid: str = "job-1", **overrides) -> NavetJobListing:
    base = {
        "uid": uid,
        "title": "Sommerjobb 2027",
        "kind": "Sommerjobb",
        "teaser": "Bli med",
        "description_html": "<p>Vi søker studenter</p>",
        "application_url": "https://example.com/apply",
        "deadline": NOW + timedelta(days=30),
        "company_id": "company-1",
        "company": "Bekk",
        "url": f"https://ifinavet.no/job/{uid}",
        "created": NOW - timedelta(days=1),
    }
    return NavetJobListing(**{**base, **overrides})


def parse(body: bytes) -> ET.Element:
    return ET.fromstring(body)


def test_produces_a_parseable_atom_document(settings):
    feed = parse(build_jobs_atom([make_job()], settings, now=NOW))

    assert feed.tag == f"{{{ATOM_NS}}}feed"
    assert feed.find("a:id", NS) is not None
    assert feed.find("a:updated", NS) is not None
    assert len(feed.findall("a:entry", NS)) == 1


def test_an_empty_feed_is_still_valid(settings):
    feed = parse(build_jobs_atom([], settings, now=NOW))

    assert feed.findall("a:entry", NS) == []


def test_entries_are_newest_posting_first(settings):
    listings = [
        make_job("old", created=NOW - timedelta(days=30)),
        make_job("new", created=NOW - timedelta(hours=1)),
        make_job("middle", created=NOW - timedelta(days=5)),
    ]

    feed = parse(build_jobs_atom(listings, settings, now=NOW))
    ids = [entry.find("a:id", NS).text for entry in feed.findall("a:entry", NS)]

    assert ids == [
        "tag:ifinavet.no,2026:job/new",
        "tag:ifinavet.no,2026:job/middle",
        "tag:ifinavet.no,2026:job/old",
    ]


def test_entry_ids_are_absolute_iris(settings):
    """RFC 4287 requires an IRI, and readers key 'already seen' on this value."""
    feed = parse(build_jobs_atom([make_job()], settings, now=NOW))

    entry_id = feed.find("a:entry/a:id", NS).text
    assert entry_id.startswith("tag:ifinavet.no,")
    assert ":" in entry_id.split("tag:")[1]


def test_entry_ids_are_stable_across_rebuilds(settings):
    """A changing id makes a reader show the same listing as new every hour."""
    listing = make_job()

    first = parse(build_jobs_atom([listing], settings, now=NOW))
    second = parse(build_jobs_atom([listing], settings, now=NOW + timedelta(hours=1)))

    assert first.find("a:entry/a:id", NS).text == second.find("a:entry/a:id", NS).text


def test_entry_links_to_the_application(settings):
    feed = parse(build_jobs_atom([make_job()], settings, now=NOW))
    link = feed.find("a:entry/a:link", NS)

    assert link.get("href") == "https://example.com/apply"


def test_feed_is_capped(settings):
    object.__setattr__(settings, "feed_max_items", 3)
    listings = [make_job(f"job-{index}", created=NOW - timedelta(days=index)) for index in range(20)]

    feed = parse(build_jobs_atom(listings, settings, now=NOW))

    assert len(feed.findall("a:entry", NS)) == 3


def test_markup_in_upstream_text_cannot_break_the_document(settings):
    """Upstream titles are free text; a bare & would produce unparseable XML."""
    listing = make_job(
        title="Utvikler <script>alert(1)</script> & mer",
        company="A & B",
        teaser="1 < 2 & 3 > 2",
    )

    feed = parse(build_jobs_atom([listing], settings, now=NOW))
    title = feed.find("a:entry/a:title", NS)

    assert "&" in title.text
    assert "<script>" in title.text
    # It round-tripped as text: the parser saw no child element, so the markup
    # was escaped on the way out rather than emitted as structure.
    assert list(title) == []


def test_content_is_declared_as_text_not_html(settings):
    """It holds the plain-text conversion, so a reader must not parse it as HTML."""
    feed = parse(build_jobs_atom([make_job()], settings, now=NOW))

    assert feed.find("a:entry/a:content", NS).get("type") == "text"


def test_content_carries_the_useful_facts(settings):
    feed = parse(build_jobs_atom([make_job()], settings, now=NOW))
    content = feed.find("a:entry/a:content", NS).text

    assert "Bekk" in content
    assert "Søknadsfrist" in content
    assert "Vi søker studenter" in content


def test_listing_type_becomes_a_category(settings):
    feed = parse(build_jobs_atom([make_job()], settings, now=NOW))

    assert feed.find("a:entry/a:category", NS).get("term") == "Sommerjobb"
