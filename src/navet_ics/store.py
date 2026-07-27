"""Holds the current feed and refreshes it in the background.

The served document is always the last successfully built one. If upstream is
unavailable the previous feed keeps being served (marked stale) rather than
disappearing, because a subscriber that receives an empty calendar would archive
every imported event.
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

from .config import Settings
from .feed import build_calendar
from .upstream import ConvexClient, UpstreamError, fetch_events

log = logging.getLogger(__name__)

_FEED_FILENAME = "calendar.ics"


@dataclass(frozen=True)
class Snapshot:
    body: bytes
    etag: str
    generated_at: datetime
    event_count: int


def _etag_for(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()[:32]}"'


class FeedStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = ConvexClient(settings)
        self._snapshot: Snapshot | None = None
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
    def snapshot(self) -> Snapshot | None:
        return self._snapshot

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
        """
        previous = self._snapshot
        if previous is None or previous.event_count == 0:
            return

        floor = previous.event_count * self._settings.min_event_ratio
        if new_count < floor:
            raise UpstreamError(
                f"refusing to publish {new_count} events after {previous.event_count} "
                f"(below {self._settings.min_event_ratio:.0%} of the previous feed); "
                "upstream is probably broken, keeping the last good feed"
            )

    async def refresh(self) -> Snapshot:
        """Fetch upstream and rebuild the feed. Raises on failure."""
        async with self._lock:
            self.last_attempt = datetime.now(tz=UTC)
            events = await fetch_events(self._settings, self._client)
            self._reject_implausible_drop(len(events))
            body = build_calendar(events, self._settings)
            snapshot = Snapshot(
                body=body,
                etag=_etag_for(body),
                generated_at=datetime.now(tz=UTC).replace(microsecond=0),
                event_count=len(events),
            )
            self._snapshot = snapshot
            self.last_success = snapshot.generated_at
            self.last_error = None
            self._save_to_disk(body)
            log.info("feed rebuilt: %d events, %d bytes", snapshot.event_count, len(body))
            return snapshot

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

    def _feed_path(self) -> Path:
        return Path(self._settings.state_dir) / _FEED_FILENAME

    def _load_from_disk(self) -> None:
        """Serve the previous feed immediately after a restart."""
        path = self._feed_path()
        # read and stat together: the file can disappear between the two calls.
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

        self._snapshot = Snapshot(
            body=body,
            etag=_etag_for(body),
            generated_at=mtime,
            event_count=body.count(b"BEGIN:VEVENT"),
        )
        # The mtime is a real success timestamp: it is when this feed was last
        # built from live data. Treating it as such means a restart is immediately
        # ready (it is genuinely serving good data) and staleness still ages out
        # normally, instead of reporting 503 until the first refresh lands.
        self.last_success = mtime
        log.info("loaded cached feed from %s (%d events)", path, self._snapshot.event_count)

    def _save_to_disk(self, body: bytes) -> None:
        """Atomically persist the feed so a restart never serves an empty calendar."""
        directory = Path(self._settings.state_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".calendar-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self._feed_path())
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            # Persistence is a convenience; the in-memory snapshot still serves.
            log.warning("could not persist feed to %s: %s", directory, exc)
