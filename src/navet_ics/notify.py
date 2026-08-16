"""Announces new job listings and newly opened registrations.

Nothing here polls: the hourly refresh already fetches everything, so "new" is
a set difference against what the previous refresh saw. That keeps the
notification cost at zero extra requests against Navet's backend, and bounds
the delay to one refresh interval.

Delivery is deliberately best-effort. A record is marked as announced whether
or not the webhook accepted it, because the alternative is a webhook that comes
back after a day of downtime and floods the channel with everything it missed.
The Atom feed is the durable side of this: it is pull-based, so it cannot miss
anything even when the webhook does.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .upstream import Dataset, NavetEvent, NavetJobListing

log = logging.getLogger(__name__)

_STATE_FILENAME = "notified.json"
_STATE_VERSION = 1


def _redacted(url: str) -> str:
    """A webhook URL identifies its channel by a secret path; never log the path."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/…" if parts.netloc else "<invalid url>"


@dataclass(frozen=True)
class Announcement:
    """One message to post, plus the key that stops it being posted twice."""

    key: str
    text: str
    # Kept alongside the rendered text so the generic JSON format can expose the
    # parts without a consumer having to parse the message back apart.
    company: str
    title: str
    url: str


@dataclass
class NotificationState:
    """Which records have already been announced.

    `bootstrapped` is what stops a fresh install, or the first run after this
    feature ships, from announcing every listing Navet has ever posted.
    """

    bootstrapped: bool = False
    seen: set[str] = field(default_factory=set)

    def to_json(self) -> str:
        return json.dumps({"version": _STATE_VERSION, "bootstrapped": self.bootstrapped, "seen": sorted(self.seen)})

    @classmethod
    def from_json(cls, raw: bytes) -> NotificationState:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
            raise ValueError(f"unsupported notification state version {payload.get('version')!r}")
        seen = payload.get("seen")
        if not isinstance(seen, list):
            raise ValueError("notification state has no usable 'seen' list")
        return cls(bootstrapped=payload.get("bootstrapped") is True, seen={str(item) for item in seen})


def _job_announcement(listing: NavetJobListing) -> Announcement:
    company = listing.company or "Ukjent bedrift"
    url = listing.application_url or listing.url
    return Announcement(
        key=f"job:{listing.uid}",
        text=f"Ny stillingsannonse fra {company}\n{listing.title}\nSøk her: {url}",
        company=company,
        title=listing.title,
        url=url,
    )


def _registration_announcement(event: NavetEvent) -> Announcement:
    company = event.company or event.title
    url = event.external_url or event.url
    return Announcement(
        key=f"registration:{event.uid}",
        text=f"Påmelding åpen for {company} ({url})",
        company=company,
        title=event.title,
        url=url,
    )


def plan(
    settings: Settings,
    dataset: Dataset,
    state: NotificationState,
    *,
    now: datetime | None = None,
) -> list[Announcement]:
    """Decide what to announce, and record it as announced.

    Mutates `state`, including pruning keys for records that have fallen out of
    the dataset, so the file cannot grow without bound.
    """
    moment = now or datetime.now(tz=UTC)
    candidates: list[Announcement] = []

    if settings.notify_new_jobs:
        candidates.extend(_job_announcement(listing) for listing in dataset.job_listings)

    if settings.notify_registration_open:
        # Registration that has *opened*, recently. The lower bound matters more
        # than it looks: without it, the first time a new semester's events load
        # we would announce every registration that opened months ago.
        earliest = moment - timedelta(hours=settings.notify_registration_window_hours)
        candidates.extend(
            _registration_announcement(event)
            for event in dataset.events
            if event.registration_opens is not None and earliest <= event.registration_opens <= moment
        )

    live_keys = {candidate.key for candidate in candidates}
    _prune(state, live_keys)

    if not state.bootstrapped:
        # First run: adopt the world as it is, silently.
        state.bootstrapped = True
        state.seen |= live_keys
        log.info("notification state seeded with %d existing record(s); announcing none", len(live_keys))
        return []

    fresh = [candidate for candidate in candidates if candidate.key not in state.seen]
    state.seen |= live_keys
    return fresh


def _prune(state: NotificationState, live_keys: set[str]) -> None:
    """Drop keys for records that have genuinely fallen out of the dataset.

    Done per kind, and only for a kind that produced something this round. The
    two ways a kind can come up empty without anything having been withdrawn are
    both real: job listings are enriched best-effort, so a failed upstream query
    yields an empty list, and either kind can simply be switched off. Pruning on
    that would make the *next* healthy refresh treat every record as new and
    announce the lot, which is the exact flood this state file exists to
    prevent. Keeping a stale key costs a few bytes; dropping one costs a channel
    full of history.
    """
    for prefix in ("job:", "registration:"):
        live = {key for key in live_keys if key.startswith(prefix)}
        if not live:
            continue
        state.seen = {key for key in state.seen if not key.startswith(prefix)} | live & state.seen


class Notifier:
    """Owns the notification state file and the outgoing webhook."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._state = NotificationState()
        self._client: httpx.AsyncClient | None = None

    # ---- state -----------------------------------------------------------

    @property
    def state(self) -> NotificationState:
        return self._state

    def _path(self) -> Path:
        return Path(self._settings.state_dir) / _STATE_FILENAME

    def load(self) -> None:
        path = self._path()
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            # No file means a fresh install, and plan() seeds rather than
            # announces. Leave bootstrapped false so that happens.
            return
        except OSError as exc:
            log.warning("could not read notification state %s: %s", path, exc)
            return

        try:
            self._state = NotificationState.from_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            # An unreadable state file would otherwise re-announce everything.
            # Re-seeding quietly is the safe failure: one missed batch of pings
            # beats flooding the channel with a semester of history.
            log.warning("notification state %s is unusable, re-seeding quietly: %s", path, exc)
            self._state = NotificationState(bootstrapped=False)

    def save(self) -> None:
        directory = Path(self._settings.state_dir)
        payload = self._state.to_json().encode("utf-8")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".notified-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self._path())
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            # Losing this means the next start re-seeds silently, not a flood.
            log.warning("could not persist notification state to %s: %s", directory, exc)

    # ---- delivery --------------------------------------------------------

    def _format(self) -> str:
        configured = self._settings.notify_webhook_format
        if configured != "auto":
            return configured
        host = urlsplit(self._settings.notify_webhook_url).netloc.lower()
        if host.endswith("slack.com"):
            return "slack"
        if host.endswith(("discord.com", "discordapp.com")):
            return "discord"
        return "json"

    def _payload(self, announcement: Announcement) -> dict:
        style = self._format()
        if style == "slack":
            return {"text": announcement.text}
        if style == "discord":
            # Discord rejects a message body over 2000 characters outright.
            return {"content": announcement.text[:1900]}
        return {
            "text": announcement.text,
            "company": announcement.company,
            "title": announcement.title,
            "url": announcement.url,
        }

    async def deliver(self, announcements: list[Announcement]) -> None:
        """Post each announcement to the configured webhook. Never raises.

        One request per announcement, so each lands in Slack as its own message
        rather than as a wall of text. Capped per refresh: if upstream ever
        publishes fifty listings at once, a channel does not want fifty pings.
        """
        url = self._settings.notify_webhook_url
        if not url or not announcements:
            return

        shown = announcements[: self._settings.notify_max_items]
        if len(announcements) > len(shown):
            log.warning(
                "capping announcements at %d; %d not sent this refresh",
                self._settings.notify_max_items,
                len(announcements) - len(shown),
            )

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.notify_timeout),
                headers={"User-Agent": self._settings.user_agent},
                follow_redirects=False,
            )

        delivered = 0
        for announcement in shown:
            try:
                response = await self._client.post(url, json=self._payload(announcement))
            except httpx.HTTPError as exc:
                log.warning("notification webhook %s failed: %s", _redacted(url), exc)
                # One failure usually means the whole endpoint is down; stop
                # rather than spending the timeout budget on each remaining item.
                return
            if response.status_code >= 400:
                log.warning("notification webhook %s returned HTTP %d", _redacted(url), response.status_code)
                return
            delivered += 1

        log.info("announced %d record(s) to %s", delivered, _redacted(url))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
