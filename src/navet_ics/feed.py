"""Builds the iCalendar documents served to subscribers.

Three feeds are produced from the same dataset:

* the events feed, which is the one Peoply imports and therefore the one whose
  UID stability and field lengths are correctness issues rather than polish;
* a registration-opening feed, kept separate precisely so that it does not
  double the number of events an importer sees; and
* a job-deadline feed.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from icalendar import Alarm, Calendar, Event, vText, vUri

from .config import Settings
from .htmltext import html_to_text
from .upstream import NavetEvent, NavetJobListing

PRODID = "-//Navet//navet-calendar-proxy//NO"

# UIDs must be globally unique and stable across refreshes so subscribers update
# rather than duplicate events. The Convex document id is already stable.
UID_DOMAIN = "ifinavet.no"

# Suffixes keep the derived feeds' UIDs from colliding with the events feed's,
# for anyone who subscribes to more than one of them in the same calendar.
REGISTRATION_UID_SUFFIX = "-registration"
JOB_UID_SUFFIX = "-deadline"

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


def _local(moment: datetime, settings: Settings) -> str:
    """Render an instant in the calendar's own timezone, for human-readable text.

    Only used inside DESCRIPTION bodies. The actual DTSTART/DTEND properties stay
    in UTC, where no timezone database lookup can make clients disagree.
    """
    try:
        from zoneinfo import ZoneInfo

        moment = moment.astimezone(ZoneInfo(settings.timezone))
    except Exception:
        moment = moment.astimezone(UTC)
    return moment.strftime("%d.%m.%Y kl. %H:%M")


def _description(event: NavetEvent, settings: Settings) -> str:
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
    if event.registration_opens:
        facts.append(f"Påmelding åpner: {_local(event.registration_opens, settings)}")
    if event.organizers:
        facts.append("Ansvarlig: " + ", ".join(o.name for o in event.organizers))
    if facts:
        blocks.append("\n".join(facts))

    body = html_to_text(event.description_html)
    if body and body != event.teaser:
        blocks.append(body)

    blocks.append(f"Påmelding og mer info: {event.url}")
    if event.external_url:
        blocks.append(f"Ekstern påmelding: {event.external_url}")

    return "\n\n".join(block for block in blocks if block)


def _job_description(listing: NavetJobListing, settings: Settings) -> str:
    blocks: list[str] = []
    if listing.teaser:
        blocks.append(listing.teaser)

    facts = [f"Bedrift: {listing.company}", f"Søknadsfrist: {_local(listing.deadline, settings)}"]
    if listing.kind:
        facts.insert(1, f"Type: {listing.kind}")
    blocks.append("\n".join(facts))

    body = html_to_text(listing.description_html)
    if body and body != listing.teaser:
        blocks.append(body)

    blocks.append(f"Utlysning: {listing.url}")
    if listing.application_url:
        blocks.append(f"Søk her: {listing.application_url}")
    return "\n\n".join(block for block in blocks if block)


def _sequence(*parts: str) -> int:
    """A content-derived SEQUENCE so clients notice edits without a real revision field.

    Upstream exposes no revision counter, so we derive a small stable integer from
    the rendered content. Any change to the event changes the number, which is all
    a client needs to treat the VEVENT as updated.
    """
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:3], "big")


def _shell(settings: Settings, name: str, description: str) -> Calendar:
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-caldesc", description)
    cal.add("x-wr-timezone", settings.timezone)
    # Both spellings exist in the wild; emit each so more clients honour the TTL.
    hours = max(1, settings.refresh_interval // 3600)
    cal.add("refresh-interval;value=duration", f"PT{hours}H")
    cal.add("x-published-ttl", f"PT{hours}H")
    return cal


def _alarm(settings: Settings, text: str) -> Alarm | None:
    """A display reminder ahead of an instant, for the two derived feeds.

    The events feed gets none: the point of a reminder is the moment you have to
    act, and an event you already have in your calendar is not that.
    """
    if not settings.reminder_alarm_minutes:
        return None
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", text)
    alarm.add("trigger", timedelta(minutes=-settings.reminder_alarm_minutes))
    return alarm


def _add_image(item: Event, url: str | None, media: str | None) -> None:
    """RFC 7986 IMAGE. FMTTYPE is set because clients use it to decide renderability.

    Note the vUri: passing a bare str here makes icalendar drop `parameters`
    entirely, emitting a bare `IMAGE:<url>` with no VALUE or FMTTYPE.
    """
    if not url:
        return
    parameters = {"VALUE": "URI"} | ({"FMTTYPE": media} if media else {})
    item.add("image", vUri(url), parameters=parameters)


def build_calendar(events: list[NavetEvent], settings: Settings, *, now: datetime | None = None) -> bytes:
    """Render `events` as an RFC 5545 VCALENDAR. This is the feed Peoply imports."""
    stamp = (now or datetime.now(tz=UTC)).replace(microsecond=0)
    cal = _shell(settings, settings.calendar_name, settings.calendar_description)
    duration = timedelta(minutes=settings.default_duration_minutes)

    for event in events:
        description = _description(event, settings)

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
        # Upstream exposes no edit timestamp, only a creation time, so this cannot
        # track real edits. SEQUENCE below is the signal that actually changes when
        # an event is edited; clients relying on LAST-MODIFIED will not see edits.
        item.add("last-modified", event.created)
        item.add("sequence", _sequence(event.title, event.start.isoformat(), event.location, description))
        item.add("status", "CONFIRMED")
        item.add("transp", "OPAQUE")
        item.add("categories", ["Navet"])
        if event.location:
            item["location"] = vText(_clip(event.location, MAX_LOCATION))
        if event.company:
            # Not ORGANIZER: that property requires a CAL-ADDRESS (mailto:), and upstream
            # exposes no organizer email. A custom property keeps strict parsers happy.
            item["x-navet-company"] = vText(event.company)
        _add_image(item, event.image_url, event.image_type)

        cal.add_component(item)

    return cal.to_ical()


def build_registration_calendar(events: list[NavetEvent], settings: Settings, *, now: datetime | None = None) -> bytes:
    """Render each event's registration opening as its own VEVENT.

    Deliberately a separate document rather than extra VEVENTs in the main feed:
    an importer like Peoply would otherwise take every one of these for a real
    event, doubling what shows up on the platform.
    """
    stamp = (now or datetime.now(tz=UTC)).replace(microsecond=0)
    cal = _shell(settings, settings.registration_calendar_name, settings.registration_calendar_description)
    duration = timedelta(minutes=settings.reminder_duration_minutes)

    for event in events:
        if event.registration_opens is None:
            continue

        summary = _clip(f"Påmelding åpner: {event.title}", MAX_SUMMARY)
        description = "\n\n".join(
            block
            for block in (
                f"Påmeldingen til «{event.title}» åpner {_local(event.registration_opens, settings)}.",
                f"Arrangementet starter {_local(event.start, settings)}."
                + (f" Plasser: {event.participation_limit}." if event.participation_limit else ""),
                f"Meld deg på: {event.external_url or event.url}",
            )
            if block
        )

        item = Event()
        item.add("uid", f"{event.uid}{REGISTRATION_UID_SUFFIX}@{UID_DOMAIN}")
        item.add("dtstamp", stamp)
        item.add("dtstart", event.registration_opens)
        item.add("dtend", event.registration_opens + duration)
        item.add("summary", summary)
        item.add("description", description)
        item.add("url", event.external_url or event.url)
        item.add("created", event.created)
        item.add("last-modified", event.created)
        item.add("sequence", _sequence(event.title, event.registration_opens.isoformat(), description))
        item.add("status", "CONFIRMED")
        # TRANSPARENT: a registration opening should not make you look busy.
        item.add("transp", "TRANSPARENT")
        item.add("categories", ["Navet", "Påmelding"])
        if event.company:
            item["x-navet-company"] = vText(event.company)
        _add_image(item, event.image_url, event.image_type)
        if (alarm := _alarm(settings, summary)) is not None:
            item.add_component(alarm)

        cal.add_component(item)

    return cal.to_ical()


def build_jobs_calendar(listings: list[NavetJobListing], settings: Settings, *, now: datetime | None = None) -> bytes:
    """Render each job listing's application deadline as a VEVENT."""
    stamp = (now or datetime.now(tz=UTC)).replace(microsecond=0)
    cal = _shell(settings, settings.jobs_calendar_name, settings.jobs_calendar_description)
    duration = timedelta(minutes=settings.reminder_duration_minutes)

    for listing in listings:
        summary = _clip(f"Søknadsfrist: {listing.title} ({listing.company})", MAX_SUMMARY)
        description = _job_description(listing, settings)

        item = Event()
        item.add("uid", f"{listing.uid}{JOB_UID_SUFFIX}@{UID_DOMAIN}")
        item.add("dtstamp", stamp)
        item.add("dtstart", listing.deadline)
        item.add("dtend", listing.deadline + duration)
        item.add("summary", summary)
        item.add("description", description)
        item.add("url", listing.application_url or listing.url)
        item.add("created", listing.created)
        item.add("last-modified", listing.created)
        item.add("sequence", _sequence(listing.title, listing.deadline.isoformat(), description))
        item.add("status", "CONFIRMED")
        item.add("transp", "TRANSPARENT")
        item.add("categories", ["Navet", "Stilling"] + ([listing.kind] if listing.kind else []))
        item["x-navet-company"] = vText(listing.company)
        _add_image(item, listing.image_url, listing.image_type)
        if (alarm := _alarm(settings, summary)) is not None:
            item.add_component(alarm)

        cal.add_component(item)

    return cal.to_ical()
