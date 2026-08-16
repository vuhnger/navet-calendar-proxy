"""Holds the current dataset and refreshes it in the background.

Everything served is the last successfully built version. If upstream is
unavailable the previous data keeps being served (marked stale) rather than
disappearing, because a subscriber that receives an empty calendar would archive
every imported event.

Request handlers never touch upstream: they read what this store already has.
That keeps response times independent of Convex, and keeps our call volume
against somebody else's backend proportional to time rather than to traffic.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import random
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from .config import Settings
from .feed import build_calendar, build_jobs_calendar, build_registration_calendar
from .models import DatasetDocument
from .upstream import ConvexClient, Dataset, UpstreamCaches, UpstreamError, fetch_dataset

log = logging.getLogger(__name__)

_STATE_FILENAME = "dataset.json"
# Written by versions that only had the events feed. Still read on startup so an
# upgrade does not begin by serving nothing.
_LEGACY_FEED_FILENAME = "calendar.ics"

EVENTS_FEED = "events"
REGISTRATIONS_FEED = "registrations"
JOBS_FEED = "jobs"

FEED_PATHS = {
    EVENTS_FEED: "/calendar.ics",
    REGISTRATIONS_FEED: "/registrations.ics",
    JOBS_FEED: "/jobs.ics",
}


@dataclass(frozen=True)
class Snapshot:
    body: bytes
    etag: str
    generated_at: datetime
    event_count: int


def _etag_for(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()[:32]}"'


def _snapshot(body: bytes, generated_at: datetime) -> Snapshot:
    return Snapshot(
        body=body,
        etag=_etag_for(body),
        generated_at=generated_at,
        event_count=body.count(b"BEGIN:VEVENT"),
    )


class FeedStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = ConvexClient(settings)
        self._caches = UpstreamCaches()
        self._document: DatasetDocument | None = None
        self._feeds: dict[str, Snapshot] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.last_error: str | None = None
        self.last_success: datetime | None = None
        self.last_attempt: datetime | None = None

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._load_from_disk()
        self._task = asyncio.create_task(self._run(), name="navet-ics-refresh")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            # Cancelling our own task is the expected outcome here, not a fault.
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._client.aclose()

    # ---- state -----------------------------------------------------------

    @property
    def document(self) -> DatasetDocument | None:
        """The current dataset in its published JSON shape, or None before the first build."""
        return self._document

    @property
    def feeds(self) -> dict[str, Snapshot]:
        return dict(self._feeds)

    def feed(self, key: str) -> Snapshot | None:
        return self._feeds.get(key)

    @property
    def snapshot(self) -> Snapshot | None:
        """The events feed: the one whose absence means we have nothing to serve."""
        return self._feeds.get(EVENTS_FEED)

    @property
    def is_stale(self) -> bool:
        if self.last_success is None:
            return True
        age = (datetime.now(tz=UTC) - self.last_success).total_seconds()
        return age > self._settings.stale_after

    # ---- refresh ---------------------------------------------------------

    def _reject_implausible_drop(self, new_count: int) -> None:
        """Refuse a refresh that loses most of the events for no visible reason.

        The dangerous upstream failure is not an outage — that raises, and we keep
        serving the last good feed. It is a *successful* response that is empty or
        truncated: schema drift, a renamed field, a backend bug that stops setting
        `published`. That sails through every structural check, and a subscriber
        that stops seeing a UID archives the event. So treat a large unexplained
        drop as a failure rather than as news.

        Only the events feed is guarded. The other two are derived from data that
        legitimately empties out — every registration opening and every deadline
        eventually passes — so the same rule there would fire on healthy data.
        """
        previous = self._feeds.get(EVENTS_FEED)
        if previous is None or previous.event_count == 0:
            return

        floor = previous.event_count * self._settings.min_event_ratio
        if new_count < floor:
            raise UpstreamError(
                f"refusing to publish {new_count} events after {previous.event_count} "
                f"(below {self._settings.min_event_ratio:.0%} of the previous feed); "
                "upstream is probably broken, keeping the last good feed"
            )

    def _build(self, dataset: Dataset, generated_at: datetime) -> dict[str, Snapshot]:
        return {
            EVENTS_FEED: _snapshot(build_calendar(dataset.events, self._settings), generated_at),
            REGISTRATIONS_FEED: _snapshot(build_registration_calendar(dataset.events, self._settings), generated_at),
            JOBS_FEED: _snapshot(build_jobs_calendar(dataset.job_listings, self._settings), generated_at),
        }

    async def refresh(self) -> Snapshot:
        """Fetch upstream and rebuild everything. Raises on failure."""
        async with self._lock:
            self.last_attempt = datetime.now(tz=UTC)
            dataset = await fetch_dataset(self._settings, self._client, self._caches)
            self._reject_implausible_drop(len(dataset.events))

            generated_at = datetime.now(tz=UTC).replace(microsecond=0)
            document = DatasetDocument.from_domain(dataset, generated_at)
            feeds = self._build(dataset, generated_at)

            self._document = document
            self._feeds = feeds
            self.last_success = generated_at
            self.last_error = None
            self._save_to_disk(document)

            log.info(
                "rebuilt: %s",
                ", ".join(f"{key} {snap.event_count} events / {len(snap.body)} bytes" for key, snap in feeds.items()),
            )
            return feeds[EVENTS_FEED]

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Deliberately broad: the refresh loop must survive any upstream
                # fault, or one bad response would stop the feed updating forever.
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.error("feed refresh failed: %s", self.last_error)

            delay = self._settings.refresh_interval + random.uniform(0, self._settings.refresh_jitter)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except TimeoutError:
                continue

    # ---- persistence -----------------------------------------------------

    def _state_path(self) -> Path:
        return Path(self._settings.state_dir) / _STATE_FILENAME

    def _load_from_disk(self) -> None:
        """Serve the previous data immediately after a restart."""
        if self._load_state():
            return
        self._load_legacy_feed()

    def _load_state(self) -> bool:
        path = self._state_path()
        try:
            raw = path.read_bytes()
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("could not read cached state %s: %s", path, exc)
            return False

        try:
            document = DatasetDocument.model_validate_json(raw)
        except ValidationError as exc:
            # A state file this process cannot parse is one an older or newer
            # version wrote. Refreshing will replace it; refusing to start would
            # be worse.
            log.warning("cached state %s is not usable, ignoring: %s", path, exc)
            return False

        self._document = document
        self._feeds = self._build(document.to_domain(), document.generated_at)
        # The mtime is a real success timestamp: it is when this data was last
        # built from live data. Treating it as such means a restart is immediately
        # ready (it is genuinely serving good data) and staleness still ages out
        # normally, instead of reporting 503 until the first refresh lands.
        self.last_success = mtime
        log.info(
            "loaded cached state from %s (%d events, %d companies, %d job listings)",
            path,
            len(document.events),
            len(document.companies),
            len(document.job_listings),
        )
        return True

    def _load_legacy_feed(self) -> None:
        """Read the pre-dataset calendar.ics, so an upgrade never starts empty.

        Only the events feed can be recovered this way, and the JSON endpoints
        stay empty until the first refresh — which is seconds away at startup.
        """
        path = Path(self._settings.state_dir) / _LEGACY_FEED_FILENAME
        try:
            body = path.read_bytes()
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("could not read cached feed %s: %s", path, exc)
            return

        if not body.startswith(b"BEGIN:VCALENDAR"):
            log.warning("cached feed %s is not a calendar, ignoring", path)
            return

        self._feeds = {EVENTS_FEED: _snapshot(body, mtime)}
        self.last_success = mtime
        log.info("loaded legacy cached feed from %s (%d events)", path, self._feeds[EVENTS_FEED].event_count)

    def _save_to_disk(self, document: DatasetDocument) -> None:
        """Atomically persist the dataset so a restart never serves an empty calendar."""
        directory = Path(self._settings.state_dir)
        payload = document.model_dump_json().encode("utf-8")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".dataset-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self._state_path())
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            # Persistence is a convenience; the in-memory snapshot still serves.
            log.warning("could not persist state to %s: %s", directory, exc)
