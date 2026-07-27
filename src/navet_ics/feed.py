"""Builds the iCalendar document served to subscribers."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from icalendar import Calendar, Event, vText

from .config import Settings
from .htmltext import html_to_text
from .upstream import NavetEvent

PRODID = "-//Navet//navet-calendar-proxy//NO"

# UIDs must be globally unique and stable across refreshes so subscribers update
# rather than duplicate events. The Convex document id is already stable.
UID_DOMAIN = "ifinavet.no"

# Consumers persist SUMMARY and LOCATION into bounded columns (Peoply uses
# varchar(150)/varchar(100)); overlong values fail their import outright.
MAX_SUMMARY = 150
MAX_LOCATION = 100


def _clip(value: str, limit: int) -> str:
    """Trim to `limit` characters on a word boundary where possible."""
    if len(value) <= limit:
        return value
    head = value[: limit - 1]
    cut = head.rfind(" ")
    if cut > limit // 2:
        head = head[:cut]
    return head.rstrip(" ,.;:-") + "…"


def _description(event: NavetEvent) -> str:
    """Human-readable body: teaser, details, full text, and a link back to the site."""
    blocks: list[str] = []

    if event.teaser:
        blocks.append(event.teaser)

    facts: list[str] = []
    if event.company:
        facts.append(f"Arrangør: {event.company}")
    if event.food:
        facts.append(f"Servering: {event.food}")
    if event.language:
        facts.append(f"Språk: {event.language}")
    if event.age_restriction:
        facts.append(f"Aldersgrense: {event.age_restriction}")
    if event.participation_limit:
        facts.append(f"Plasser: {event.participation_limit}")
    if facts:
        blocks.append("\n".join(facts))

    body = html_to_text(event.description_html)
    if body and body != event.teaser:
        blocks.append(body)

    blocks.append(f"Påmelding og mer info: {event.url}")
    if event.external_url:
        blocks.append(f"Ekstern påmelding: {event.external_url}")

    return "\n\n".join(block for block in blocks if block)


def _sequence(event: NavetEvent, description: str) -> int:
    """A content-derived SEQUENCE so clients notice edits without a real revision field.

    Upstream exposes no revision counter, so we derive a small stable integer from
    the rendered content. Any change to the event changes the number, which is all
    a client needs to treat the VEVENT as updated.
    """
    digest = hashlib.sha256(
        "\x1f".join([event.title, event.start.isoformat(), event.location, description]).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:3], "big")


def build_calendar(events: list[NavetEvent], settings: Settings, *, now: datetime | None = None) -> bytes:
    """Render `events` as an RFC 5545 VCALENDAR."""
    stamp = (now or datetime.now(tz=UTC)).replace(microsecond=0)

    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", settings.calendar_name)
    cal.add("x-wr-caldesc", settings.calendar_description)
    cal.add("x-wr-timezone", settings.timezone)
    # Both spellings exist in the wild; emit each so more clients honour the TTL.
    cal.add("refresh-interval;value=duration", f"PT{max(1, settings.refresh_interval // 3600)}H")
    cal.add("x-published-ttl", f"PT{max(1, settings.refresh_interval // 3600)}H")

    duration = timedelta(minutes=settings.default_duration_minutes)

    for event in events:
        description = _description(event)

        item = Event()
        item.add("uid", f"{event.uid}@{UID_DOMAIN}")
        item.add("dtstamp", stamp)
        # Emitted as UTC (…Z) rather than TZID so no VTIMEZONE definition is needed
        # and every client resolves the instant identically.
        item.add("dtstart", event.start)
        # Upstream stores no end time; a configurable default keeps events bounded.
        item.add("dtend", event.start + duration)
        item.add("summary", _clip(event.title, MAX_SUMMARY))
        item.add("description", description)
        item.add("url", event.url)
        item.add("created", event.created)
        item.add("last-modified", event.created)
        item.add("sequence", _sequence(event, description))
        item.add("status", "CONFIRMED")
        item.add("transp", "OPAQUE")
        item.add("categories", ["Navet"])
        if event.location:
            item["location"] = vText(_clip(event.location, MAX_LOCATION))
        if event.company:
            # Not ORGANIZER: that property requires a CAL-ADDRESS (mailto:), and upstream
            # exposes no organizer email. A custom property keeps strict parsers happy.
            item["x-navet-company"] = vText(event.company)

        cal.add_component(item)

    return cal.to_ical()
