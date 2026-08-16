"""Atom feeds, for reading new job listings in a feed reader.

The webhook in `notify` is push and best-effort; this is pull and complete. A
reader that was offline for a week still sees everything, because the document
is simply the current state of the dataset rather than a log of events anyone
had to successfully deliver.

Built with ElementTree rather than string formatting so that a title containing
`&` or `<` cannot produce a document no reader will parse.

This module only ever *builds* XML. It must stay that way: stdlib ElementTree
is safe to serialise with, but its parser is vulnerable to entity-expansion and
external-entity attacks, so anything that needs to read XML here belongs on
defusedxml instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from .config import Settings
from .htmltext import html_to_text
from .upstream import NavetJobListing

ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_TYPE = "application/atom+xml; charset=utf-8"


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(parent: ET.Element, tag: str, value: str, **attrib: str) -> ET.Element:
    element = ET.SubElement(parent, tag, attrib)
    element.text = value
    return element


def build_jobs_atom(
    listings: list[NavetJobListing],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> bytes:
    """Job listings as an Atom document, newest posting first.

    Ordered by when the listing appeared rather than by deadline: a reader wants
    to know what is new, and the deadline is a property of the entry, not its
    position in the feed.
    """
    stamp = now or datetime.now(tz=UTC)
    site = settings.site_url.rstrip("/")

    ET.register_namespace("", ATOM_NS)
    feed = ET.Element(f"{{{ATOM_NS}}}feed")

    _text(feed, f"{{{ATOM_NS}}}title", settings.jobs_feed_title)
    _text(feed, f"{{{ATOM_NS}}}subtitle", "Nye stillingsannonser utlyst gjennom Navet.")
    # The id must be a permanent, globally unique IRI for the feed itself.
    _text(feed, f"{{{ATOM_NS}}}id", f"{site}/job")
    _text(feed, f"{{{ATOM_NS}}}updated", _rfc3339(stamp))
    ET.SubElement(feed, f"{{{ATOM_NS}}}link", {"rel": "alternate", "type": "text/html", "href": f"{site}/job"})
    author = ET.SubElement(feed, f"{{{ATOM_NS}}}author")
    _text(author, f"{{{ATOM_NS}}}name", "Navet")

    newest_first = sorted(listings, key=lambda listing: (listing.created, listing.uid), reverse=True)

    for listing in newest_first[: settings.feed_max_items]:
        entry = ET.SubElement(feed, f"{{{ATOM_NS}}}entry")
        _text(entry, f"{{{ATOM_NS}}}title", f"{listing.title} hos {listing.company}")
        _text(entry, f"{{{ATOM_NS}}}id", f"{listing.uid}@ifinavet.no")
        _text(entry, f"{{{ATOM_NS}}}updated", _rfc3339(listing.created))
        _text(entry, f"{{{ATOM_NS}}}published", _rfc3339(listing.created))
        ET.SubElement(
            entry,
            f"{{{ATOM_NS}}}link",
            {"rel": "alternate", "type": "text/html", "href": listing.application_url or listing.url},
        )
        if listing.kind:
            ET.SubElement(entry, f"{{{ATOM_NS}}}category", {"term": listing.kind})

        body = html_to_text(listing.description_html)
        summary = "\n\n".join(
            block
            for block in (
                listing.teaser,
                f"Bedrift: {listing.company}\nSøknadsfrist: {listing.deadline.strftime('%d.%m.%Y')}",
                body if body and body != listing.teaser else "",
                f"Utlysning: {listing.url}",
            )
            if block
        )
        # type="text" rather than "html": the value is the plain-text conversion,
        # so declaring it as HTML would make readers interpret stray markup.
        _text(entry, f"{{{ATOM_NS}}}content", summary, type="text")

    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)
