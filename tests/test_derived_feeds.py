"""Tests for the registration and job feeds, and for the IMAGE property.

The property that matters most here is separation: the registration feed exists
as its own document so that an importer like Peoply, which turns every VEVENT
into a platform event, does not end up showing each event twice.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from icalendar import Calendar

from navet_ics.config import Settings
from navet_ics.feed import build_calendar, build_jobs_calendar, build_registration_calendar
from navet_ics.upstream import NavetEvent, NavetJobListing, NavetOrganizer


@pytest.fixture
def settings() -> Settings:
    return Settings()


def make_event(**overrides) -> NavetEvent:
    base = {
        "uid": "j97c3ym0f8t8dq611a822g18js7nejwa",
        "title": "Bedriftspresentasjon med DNV",
        "start": datetime(2026, 2, 10, 15, 0, tzinfo=UTC),
        "teaser": "Velkommen til en faglig kveld!",
        "description_html": "<p>Hei</p>",
        "location": "Veritasveien 1",
        "company": "DNV",
        "food": "Pizza",
        "language": "Norsk",
        "age_restriction": "Ingen",
        "url": "https://ifinavet.no/events/v26-dnv",
        "external_url": None,
        "created": datetime(2026, 1, 12, 18, 19, 36, tzinfo=UTC),
        "participation_limit": 40,
        "company_id": "k97asv3rjzdp52twndzrw41c1d7ncnsf",
        "registration_opens": datetime(2026, 2, 3, 11, 0, tzinfo=UTC),
    }
    return NavetEvent(**{**base, **overrides})


def make_job(**overrides) -> NavetJobListing:
    base = {
        "uid": "jn7b6ba8aygwv170dqgcss4jqs8bk2pr",
        "title": "Sommerjobb 2027",
        "kind": "Sommerjobb",
        "teaser": "Bli med oss i sommer",
        "description_html": "<p>Vi søker studenter</p>",
        "application_url": "https://example.com/apply",
        "deadline": datetime(2026, 9, 13, 21, 59, tzinfo=UTC),
        "company_id": "k974yqr3cazxy6729kzrxe2b757n0hrw",
        "company": "Bekk",
        "url": "https://ifinavet.no/job/jn7b6ba8aygwv170dqgcss4jqs8bk2pr",
        "created": datetime(2026, 1, 1, tzinfo=UTC),
    }
    return NavetJobListing(**{**base, **overrides})


def parse(body: bytes) -> list:
    return list(Calendar.from_ical(body).walk("VEVENT"))


# ---- registration feed ---------------------------------------------------


def test_registration_feed_uses_the_opening_time_not_the_event_time(settings):
    event = make_event()
    vevent = parse(build_registration_calendar([event], settings))[0]

    assert vevent["DTSTART"].dt == event.registration_opens
    assert vevent["DTSTART"].dt != event.start


def test_registration_uids_do_not_collide_with_the_events_feed(settings):
    """Both feeds may end up in the same calendar; a shared UID would merge them."""
    event = make_event()
    registration = parse(build_registration_calendar([event], settings))[0]
    calendar = parse(build_calendar([event], settings))[0]

    assert str(registration["UID"]) != str(calendar["UID"])
    assert str(registration["UID"]) == f"{event.uid}-registration@ifinavet.no"


def test_events_without_a_registration_time_are_skipped(settings):
    body = build_registration_calendar([make_event(registration_opens=None)], settings)

    assert body.startswith(b"BEGIN:VCALENDAR")
    assert parse(body) == []


def test_registration_entries_are_transparent_and_carry_an_alarm(settings):
    vevent = parse(build_registration_calendar([make_event()], settings))[0]

    # A registration opening should not make the subscriber look busy.
    assert str(vevent["TRANSP"]) == "TRANSPARENT"
    assert len(vevent.walk("VALARM")) == 1


def test_alarm_can_be_switched_off(settings):
    object.__setattr__(settings, "reminder_alarm_minutes", 0)
    vevent = parse(build_registration_calendar([make_event()], settings))[0]

    assert vevent.walk("VALARM") == []


def test_registration_links_to_external_signup_when_there_is_one(settings):
    event = make_event(external_url="https://bekk.no/arrangementer/x")
    vevent = parse(build_registration_calendar([event], settings))[0]

    assert str(vevent["URL"]) == "https://bekk.no/arrangementer/x"


# ---- events feed ---------------------------------------------------------


def test_registration_opening_appears_in_the_event_description(settings):
    """The main feed still mentions it, for subscribers who only take one feed."""
    vevent = parse(build_calendar([make_event()], settings))[0]

    # 11:00 UTC is 12:00 in Europe/Oslo, and the body is rendered for humans.
    assert "Påmelding åpner: 03.02.2026 kl. 12:00" in str(vevent["DESCRIPTION"])


def test_organizers_are_listed_by_name_only(settings):
    organizer = NavetOrganizer(
        id="x", name="Anders Rød", role="hovedansvarlig", image_url=None, email="anders@example.com"
    )
    vevent = parse(build_calendar([make_event(organizers=(organizer,))], settings))[0]
    description = str(vevent["DESCRIPTION"])

    assert "Ansvarlig: Anders Rød" in description
    # The address is upstream data we deliberately do not republish in the feed.
    assert "anders@example.com" not in description


def test_events_feed_has_no_alarms(settings):
    """An event you already have in your calendar does not need a reminder."""
    vevent = parse(build_calendar([make_event()], settings))[0]

    assert vevent.walk("VALARM") == []


# ---- images --------------------------------------------------------------


def test_image_carries_value_and_media_type(settings):
    event = make_event(image_url="https://example.com/logo.png", image_type="image/png")
    body = build_calendar([event], settings)

    line = next(line for line in body.decode().splitlines() if line.startswith("IMAGE"))
    assert "VALUE=URI" in line
    assert "FMTTYPE=image/png" in line


def test_no_image_property_when_there_is_no_logo(settings):
    body = build_calendar([make_event()], settings)

    assert "IMAGE" not in body.decode()


# ---- jobs feed -----------------------------------------------------------


def test_job_deadline_becomes_the_event_time(settings):
    listing = make_job()
    vevent = parse(build_jobs_calendar([listing], settings))[0]

    assert vevent["DTSTART"].dt == listing.deadline
    assert str(vevent["UID"]) == f"{listing.uid}-deadline@ifinavet.no"


def test_job_summary_names_the_company_and_links_to_the_application(settings):
    vevent = parse(build_jobs_calendar([make_job()], settings))[0]

    assert "Søknadsfrist" in str(vevent["SUMMARY"])
    assert "Bekk" in str(vevent["SUMMARY"])
    assert str(vevent["URL"]) == "https://example.com/apply"


def test_job_falls_back_to_the_listing_page_without_an_application_url(settings):
    listing = make_job(application_url=None)
    vevent = parse(build_jobs_calendar([listing], settings))[0]

    assert str(vevent["URL"]) == listing.url


def test_job_categories_include_the_listing_type(settings):
    vevent = parse(build_jobs_calendar([make_job()], settings))[0]

    assert "Sommerjobb" in str(vevent["CATEGORIES"].to_ical().decode())


def test_all_feeds_fold_long_lines(settings):
    """RFC 5545 caps a line at 75 octets; the derived feeds must fold too."""
    listing = make_job(description_html="<p>" + "Vi søker dyktige studenter. " * 50 + "</p>")
    event = make_event(teaser="Bli med! " * 60)

    for body in (
        build_calendar([event], settings),
        build_registration_calendar([event], settings),
        build_jobs_calendar([listing], settings),
    ):
        for line in body.split(b"\r\n"):
            assert len(line) <= 75, f"unfolded line of {len(line)} bytes: {line[:80]!r}"
