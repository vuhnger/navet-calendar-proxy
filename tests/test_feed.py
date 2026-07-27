"""Tests for the generated calendar document.

These assert the properties consumers actually depend on. Peoply archives any
imported event whose UID stops appearing, and stores SUMMARY/LOCATION in bounded
columns, so UID stability and field lengths are correctness issues, not polish.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from icalendar import Calendar

from navet_ics.config import Settings
from navet_ics.feed import MAX_LOCATION, MAX_SUMMARY, build_calendar
from navet_ics.upstream import NavetEvent


@pytest.fixture
def settings() -> Settings:
    return Settings()


def make_event(**overrides) -> NavetEvent:
    base = {
        "uid": "j97c3ym0f8t8dq611a822g18js7nejwa",
        "title": "Bedriftspresentasjon med DNV",
        "start": datetime(2026, 2, 10, 15, 0, tzinfo=UTC),
        "teaser": "Velkommen til en faglig kveld!",
        "description_html": "<p>Hei &amp; velkommen</p><p>Vi gleder oss.</p>",
        "location": "Veritasveien 1, 1363 Oslo",
        "company": "DNV",
        "food": "Pizza",
        "language": "Norsk",
        "age_restriction": "Ingen begrensning",
        "url": "https://ifinavet.no/events/v26-dnv",
        "external_url": None,
        "created": datetime(2026, 1, 12, 18, 19, 36, tzinfo=UTC),
        "participation_limit": 40,
    }
    return NavetEvent(**{**base, **overrides})


def parse(body: bytes) -> list:
    return list(Calendar.from_ical(body).walk("VEVENT"))


def test_produces_parseable_calendar(settings):
    body = build_calendar([make_event()], settings)

    # Peoply's content-type check accepts a body starting with BEGIN:VCALENDAR.
    assert body.startswith(b"BEGIN:VCALENDAR")
    assert len(parse(body)) == 1


def test_folded_lines_stay_within_rfc5545_limit(settings):
    long_description = "Vi inviterer til en kveld med mat og faglig innhold. " * 40
    body = build_calendar([make_event(description_html=f"<p>{long_description}</p>")], settings)

    for line in body.split(b"\r\n"):
        assert len(line) <= 75, f"unfolded line of {len(line)} bytes: {line[:80]!r}"


def test_uid_is_stable_and_namespaced(settings):
    event = make_event()
    first = parse(build_calendar([event], settings))[0]
    second = parse(build_calendar([event], settings))[0]

    assert str(first["UID"]) == f"{event.uid}@ifinavet.no"
    # Regenerating must not change the UID, or every refresh would archive and
    # recreate the event downstream.
    assert str(first["UID"]) == str(second["UID"])


def test_summary_and_location_are_clipped_to_consumer_limits(settings):
    event = make_event(title="Bedriftspresentasjon " * 20, location="Veritasveien 1 " * 20)
    vevent = parse(build_calendar([event], settings))[0]

    assert len(str(vevent["SUMMARY"])) <= MAX_SUMMARY
    assert len(str(vevent["LOCATION"])) <= MAX_LOCATION


def test_short_summary_is_left_untouched(settings):
    vevent = parse(build_calendar([make_event()], settings))[0]
    assert str(vevent["SUMMARY"]) == "Bedriftspresentasjon med DNV"


def test_dtend_follows_configured_duration(settings):
    vevent = parse(build_calendar([make_event()], settings))[0]

    start, end = vevent["DTSTART"].dt, vevent["DTEND"].dt
    assert (end - start).total_seconds() == settings.default_duration_minutes * 60
    assert end > start


def test_timestamps_are_timezone_aware_utc(settings):
    vevent = parse(build_calendar([make_event()], settings))[0]

    # Floating times would be interpreted in the consumer's local timezone.
    assert vevent["DTSTART"].dt.tzinfo is not None
    assert vevent["DTSTART"].dt.utcoffset().total_seconds() == 0


def test_description_is_plain_text_with_entities_decoded(settings):
    vevent = parse(build_calendar([make_event()], settings))[0]
    description = str(vevent["DESCRIPTION"])

    assert "<p>" not in description
    # Peoply strips tags but does not decode entities, so we must decode them.
    assert "&amp;" not in description
    assert "Hei & velkommen" in description
    assert "https://ifinavet.no/events/v26-dnv" in description


def test_description_includes_external_registration_when_present(settings):
    event = make_event(external_url="https://www.bekk.no/arrangementer/kickoff")
    description = str(parse(build_calendar([event], settings))[0]["DESCRIPTION"])

    assert "https://www.bekk.no/arrangementer/kickoff" in description


def test_sequence_changes_when_content_changes(settings):
    original = parse(build_calendar([make_event()], settings))[0]
    edited = parse(build_calendar([make_event(title="Nytt navn")], settings))[0]

    assert int(original["SEQUENCE"]) != int(edited["SEQUENCE"])


def test_sequence_is_stable_for_unchanged_content(settings):
    first = parse(build_calendar([make_event()], settings))[0]
    second = parse(build_calendar([make_event()], settings))[0]

    assert int(first["SEQUENCE"]) == int(second["SEQUENCE"])


def test_empty_input_still_produces_a_valid_calendar(settings):
    body = build_calendar([], settings)

    assert body.startswith(b"BEGIN:VCALENDAR")
    assert parse(body) == []


def test_missing_optional_fields_do_not_break_generation(settings):
    event = make_event(
        location="",
        company="",
        food="",
        language="",
        age_restriction="",
        teaser="",
        description_html="",
        participation_limit=None,
    )
    vevent = parse(build_calendar([event], settings))[0]

    assert "LOCATION" not in vevent
    # The permalink is the one thing a description must always carry.
    assert "https://ifinavet.no/events/v26-dnv" in str(vevent["DESCRIPTION"])
