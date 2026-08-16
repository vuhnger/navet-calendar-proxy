"""Tests for the new-listing announcements.

The failure mode worth guarding against is not a missed ping, it is a flood:
anything that makes the service forget what it has already announced turns a
Slack channel into a wall of a semester's history. So most of these are about
what must *not* be sent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from navet_ics.config import Settings
from navet_ics.notify import JOB, REGISTRATION, Announcement, NotificationState, Notifier, plan
from navet_ics.upstream import Dataset, NavetEvent, NavetJobListing

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def make_settings(**overrides) -> Settings:
    settings = Settings()
    for key, value in overrides.items():
        object.__setattr__(settings, key, value)
    return settings


def make_job(uid: str = "job-1", **overrides) -> NavetJobListing:
    base = {
        "uid": uid,
        "title": "Sommerjobb 2027",
        "kind": "Sommerjobb",
        "teaser": "Bli med",
        "description_html": "<p>Søk her</p>",
        "application_url": "https://example.com/apply",
        "deadline": NOW + timedelta(days=30),
        "company_id": "company-1",
        "company": "Bekk",
        "url": f"https://ifinavet.no/job/{uid}",
        "created": NOW - timedelta(days=1),
    }
    return NavetJobListing(**{**base, **overrides})


def make_event(uid: str = "event-1", **overrides) -> NavetEvent:
    base = {
        "uid": uid,
        "title": "Bedriftspresentasjon med Netcompany",
        "start": NOW + timedelta(days=10),
        "teaser": "",
        "description_html": "",
        "location": "OJD",
        "company": "Netcompany",
        "food": "Pizza",
        "language": "Norsk",
        "age_restriction": "",
        "url": "https://ifinavet.no/events/h26-netcompany",
        "external_url": None,
        "created": NOW - timedelta(days=20),
        "participation_limit": 40,
        "company_id": "company-2",
        "registration_opens": NOW - timedelta(hours=1),
    }
    return NavetEvent(**{**base, **overrides})


def bootstrapped() -> NotificationState:
    return NotificationState(bootstrapped=True)


# ---- seeding -------------------------------------------------------------


def test_first_run_announces_nothing():
    """Otherwise switching this on would post every listing Navet has ever had."""
    settings = make_settings()
    dataset = Dataset(events=[make_event()], job_listings=[make_job(), make_job("job-2")])
    state = NotificationState()

    assert plan(settings, dataset, state, now=NOW) == []
    assert state.bootstrapped is True
    assert len(state.seen) == 3


def test_second_run_after_seeding_is_also_quiet():
    settings = make_settings()
    dataset = Dataset(events=[make_event()], job_listings=[make_job()])
    state = NotificationState()

    plan(settings, dataset, state, now=NOW)

    assert plan(settings, dataset, state, now=NOW) == []


# ---- new job listings ----------------------------------------------------


def test_a_new_listing_is_announced_once():
    settings = make_settings()
    state = bootstrapped()
    state.seen.add("job:job-1")
    dataset = Dataset(job_listings=[make_job(), make_job("job-2", title="Internship")])

    first = plan(settings, dataset, state, now=NOW)
    assert [a.key for a in first] == ["job:job-2"]

    assert plan(settings, dataset, state, now=NOW) == []


def test_job_message_matches_the_requested_shape():
    settings = make_settings()
    state = bootstrapped()
    dataset = Dataset(job_listings=[make_job()])

    announcement = plan(settings, dataset, state, now=NOW)[0]

    assert announcement.text == ("Ny stillingsannonse fra Bekk\nSommerjobb 2027\nSøk her: https://example.com/apply")


def test_job_without_an_application_url_links_to_the_listing():
    settings = make_settings()
    dataset = Dataset(job_listings=[make_job(application_url=None)])

    announcement = plan(settings, dataset, bootstrapped(), now=NOW)[0]

    assert announcement.text.endswith("Søk her: https://ifinavet.no/job/job-1")


# ---- registration openings ----------------------------------------------


def test_registration_message_matches_the_requested_shape():
    settings = make_settings()
    dataset = Dataset(events=[make_event()])

    announcement = plan(settings, dataset, bootstrapped(), now=NOW)[0]

    assert announcement.text == ("Påmelding åpen for Netcompany (https://ifinavet.no/events/h26-netcompany)")


def test_registration_that_has_not_opened_yet_is_not_announced():
    """'Påmelding åpen' has to actually be true when it is posted."""
    settings = make_settings()
    dataset = Dataset(events=[make_event(registration_opens=NOW + timedelta(hours=2))])

    assert plan(settings, dataset, bootstrapped(), now=NOW) == []


def test_registration_that_opened_long_ago_is_not_announced():
    """Loading a new semester must not announce openings from months back."""
    settings = make_settings(notify_registration_window_hours=48)
    dataset = Dataset(events=[make_event(registration_opens=NOW - timedelta(days=90))])

    assert plan(settings, dataset, bootstrapped(), now=NOW) == []


def test_event_without_a_registration_time_is_ignored():
    settings = make_settings()
    dataset = Dataset(events=[make_event(registration_opens=None)])

    assert plan(settings, dataset, bootstrapped(), now=NOW) == []


def test_registration_uses_the_external_signup_link_when_there_is_one():
    settings = make_settings()
    dataset = Dataset(events=[make_event(external_url="https://bekk.no/x")])

    assert "(https://bekk.no/x)" in plan(settings, dataset, bootstrapped(), now=NOW)[0].text


# ---- switches and bounds -------------------------------------------------


def test_each_kind_can_be_switched_off():
    dataset = Dataset(events=[make_event()], job_listings=[make_job()])

    jobs_only = plan(make_settings(notify_registration_open=False), dataset, bootstrapped(), now=NOW)
    assert [a.key for a in jobs_only] == ["job:job-1"]

    registrations_only = plan(make_settings(notify_new_jobs=False), dataset, bootstrapped(), now=NOW)
    assert [a.key for a in registrations_only] == ["registration:event-1"]


def test_state_does_not_grow_without_bound():
    """Keys for records that fell out of the dataset are pruned."""
    settings = make_settings()
    state = bootstrapped()
    state.seen |= {f"job:ancient-{index}" for index in range(100)}
    dataset = Dataset(job_listings=[make_job()])

    plan(settings, dataset, state, now=NOW)

    assert state.seen == {"job:job-1"}


def test_a_failed_job_fetch_does_not_wipe_what_was_already_announced():
    """Job listings are enriched best-effort, so a failed query looks like "none".

    Pruning on that would make the next healthy refresh announce every listing
    again, which is the flood this state exists to prevent.
    """
    settings = make_settings()
    state = bootstrapped()
    state.seen |= {"job:job-1", "job:job-2"}

    # The events fetch succeeded; the job listings one came back empty.
    plan(settings, Dataset(events=[make_event()], job_listings=[]), state, now=NOW)
    assert state.seen >= {"job:job-1", "job:job-2"}

    # ...and when it recovers, nothing is announced as new.
    recovered = Dataset(job_listings=[make_job("job-1"), make_job("job-2")])
    assert plan(settings, recovered, state, now=NOW) == []


def test_switching_a_kind_off_and_on_again_does_not_flood():
    settings_on = make_settings()
    settings_off = make_settings(notify_new_jobs=False)
    dataset = Dataset(job_listings=[make_job("job-1"), make_job("job-2")])
    state = bootstrapped()

    plan(settings_on, dataset, state, now=NOW)
    # A few refreshes with job notifications turned off...
    plan(settings_off, dataset, state, now=NOW)
    plan(settings_off, dataset, state, now=NOW)

    # ...must not make the listings look new again when it comes back on.
    assert plan(settings_on, dataset, state, now=NOW) == []


def test_pruning_one_kind_does_not_disturb_the_other():
    settings = make_settings()
    state = bootstrapped()
    state.seen |= {"job:gone", "registration:event-1"}
    dataset = Dataset(events=[make_event()], job_listings=[make_job()])

    plan(settings, dataset, state, now=NOW)

    assert "job:gone" not in state.seen
    assert "registration:event-1" in state.seen


# ---- state file ----------------------------------------------------------


def test_state_survives_a_restart(tmp_path):
    settings = make_settings(state_dir=str(tmp_path))
    dataset = Dataset(job_listings=[make_job()])

    first = Notifier(settings)
    first.load()
    plan(settings, dataset, first.state, now=NOW)
    first.save()

    # A restart must not re-announce what the previous process already sent.
    second = Notifier(settings)
    second.load()

    assert second.state.bootstrapped is True
    assert plan(settings, dataset, second.state, now=NOW) == []


def test_a_corrupt_state_file_reseeds_quietly_rather_than_flooding(tmp_path):
    settings = make_settings(state_dir=str(tmp_path))
    (tmp_path / "notified.json").write_text("{ not json at all")

    notifier = Notifier(settings)
    notifier.load()

    assert notifier.state.bootstrapped is False
    dataset = Dataset(job_listings=[make_job(), make_job("job-2")])
    assert plan(settings, dataset, notifier.state, now=NOW) == []


@pytest.mark.parametrize("content", ["[1, 2, 3]", '"a string"', "null", "42", "{ not json at all"])
def test_no_shape_of_broken_state_file_can_stop_the_service_starting(tmp_path, content):
    """load() is called during startup, so anything it raises is a failed boot."""
    settings = make_settings(state_dir=str(tmp_path))
    (tmp_path / "notified.json").write_text(content)

    notifier = Notifier(settings)
    notifier.load()

    assert notifier.state.bootstrapped is False


def test_state_file_from_an_unknown_version_reseeds_quietly(tmp_path):
    settings = make_settings(state_dir=str(tmp_path))
    (tmp_path / "notified.json").write_text(json.dumps({"version": 99, "seen": ["job:job-1"]}))

    notifier = Notifier(settings)
    notifier.load()

    assert notifier.state.bootstrapped is False


# ---- delivery ------------------------------------------------------------


def announcement(key: str = "job:job-1", kind: str = JOB) -> Announcement:
    return Announcement(
        key=key, kind=kind, text="Ny stillingsannonse fra Bekk", company="Bekk", title="X", url="https://x"
    )


class Webhook:
    def __init__(self, status: int = 200):
        self.status = status
        self.bodies: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(self.status)

    def attach(self, notifier: Notifier) -> None:
        notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


async def test_slack_format_is_used_for_slack_urls():
    settings = make_settings(notify_webhook_url="https://hooks.slack.com/services/T/B/x")
    notifier = Notifier(settings)
    hook = Webhook()
    hook.attach(notifier)

    await notifier.deliver([announcement()])

    assert hook.bodies == [{"text": "Ny stillingsannonse fra Bekk"}]
    await notifier.aclose()


async def test_discord_format_is_used_for_discord_urls():
    settings = make_settings(notify_webhook_url="https://discord.com/api/webhooks/1/x")
    notifier = Notifier(settings)
    hook = Webhook()
    hook.attach(notifier)

    await notifier.deliver([announcement()])

    assert hook.bodies == [{"content": "Ny stillingsannonse fra Bekk"}]
    await notifier.aclose()


async def test_each_announcement_is_its_own_message():
    settings = make_settings(notify_webhook_url="https://hooks.slack.com/services/T/B/x")
    notifier = Notifier(settings)
    hook = Webhook()
    hook.attach(notifier)

    await notifier.deliver([announcement("a"), announcement("b"), announcement("c")])

    assert len(hook.bodies) == 3
    await notifier.aclose()


def test_a_burst_is_capped_per_refresh():
    settings = make_settings(notify_max_items=2)
    dataset = Dataset(job_listings=[make_job(f"job-{index}") for index in range(5)])

    assert len(plan(settings, dataset, bootstrapped(), now=NOW)) == 2


def test_a_capped_burst_is_deferred_rather_than_dropped():
    """The overflow must arrive later, not be marked announced and lost."""
    settings = make_settings(notify_max_items=2)
    dataset = Dataset(job_listings=[make_job(f"job-{index}") for index in range(5)])
    state = bootstrapped()

    delivered: list[str] = []
    for _ in range(4):
        delivered.extend(a.key for a in plan(settings, dataset, state, now=NOW))

    assert sorted(delivered) == [f"job:job-{index}" for index in range(5)]
    # And once drained, it goes quiet rather than looping.
    assert plan(settings, dataset, state, now=NOW) == []


class RoutingWebhook:
    """Records which URL each message went to."""

    def __init__(self, failing: set[str] | None = None):
        self.failing = failing or set()
        self.sent: list[tuple[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in self.failing:
            return httpx.Response(500)
        self.sent.append((url, json.loads(request.content)["text"]))
        return httpx.Response(200)

    def attach(self, notifier: Notifier) -> None:
        notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


JOBS_HOOK = "https://hooks.slack.com/services/T/B/jobs"
BEDPRES_HOOK = "https://hooks.slack.com/services/T/B/bedpres"


async def test_each_kind_goes_to_its_own_channel():
    settings = make_settings(
        notify_jobs_webhook_url=JOBS_HOOK,
        notify_registration_webhook_url=BEDPRES_HOOK,
    )
    notifier = Notifier(settings)
    hook = RoutingWebhook()
    hook.attach(notifier)

    await notifier.deliver([announcement("job:1", JOB), announcement("registration:1", REGISTRATION)])

    assert {url for url, _ in hook.sent} == {JOBS_HOOK, BEDPRES_HOOK}
    assert len(hook.sent) == 2
    await notifier.aclose()


async def test_a_kind_without_its_own_channel_uses_the_shared_one():
    shared = "https://hooks.slack.com/services/T/B/shared"
    settings = make_settings(notify_webhook_url=shared, notify_jobs_webhook_url=JOBS_HOOK)
    notifier = Notifier(settings)
    hook = RoutingWebhook()
    hook.attach(notifier)

    await notifier.deliver([announcement("job:1", JOB), announcement("registration:1", REGISTRATION)])

    assert sorted(url for url, _ in hook.sent) == sorted([JOBS_HOOK, shared])
    await notifier.aclose()


async def test_one_dead_channel_does_not_silence_the_other():
    settings = make_settings(
        notify_jobs_webhook_url=JOBS_HOOK,
        notify_registration_webhook_url=BEDPRES_HOOK,
    )
    notifier = Notifier(settings)
    hook = RoutingWebhook(failing={JOBS_HOOK})
    hook.attach(notifier)

    await notifier.deliver([announcement("job:1", JOB), announcement("registration:1", REGISTRATION)])

    assert [url for url, _ in hook.sent] == [BEDPRES_HOOK]
    await notifier.aclose()


async def test_a_kind_with_no_channel_at_all_is_skipped():
    settings = make_settings(notify_jobs_webhook_url=JOBS_HOOK)
    notifier = Notifier(settings)
    hook = RoutingWebhook()
    hook.attach(notifier)

    await notifier.deliver([announcement("job:1", JOB), announcement("registration:1", REGISTRATION)])

    assert [url for url, _ in hook.sent] == [JOBS_HOOK]
    await notifier.aclose()


async def test_nothing_is_sent_without_a_configured_webhook():
    notifier = Notifier(make_settings(notify_webhook_url=""))
    hook = Webhook()
    hook.attach(notifier)

    await notifier.deliver([announcement()])

    assert hook.bodies == []
    await notifier.aclose()


@pytest.mark.parametrize("failure", [500, 404])
async def test_a_failing_webhook_does_not_raise(failure):
    settings = make_settings(notify_webhook_url="https://hooks.slack.com/services/T/B/x")
    notifier = Notifier(settings)
    Webhook(status=failure).attach(notifier)

    await notifier.deliver([announcement()])
    await notifier.aclose()


async def test_a_transport_error_does_not_raise():
    settings = make_settings(notify_webhook_url="https://hooks.slack.com/services/T/B/x")
    notifier = Notifier(settings)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(boom))

    await notifier.deliver([announcement()])
    await notifier.aclose()


async def test_the_webhook_secret_is_never_logged(caplog):
    """The path of a Slack webhook URL is the credential."""
    settings = make_settings(notify_webhook_url="https://hooks.slack.com/services/T00/B00/sUpErSeCrEt")
    notifier = Notifier(settings)
    Webhook(status=500).attach(notifier)

    with caplog.at_level("WARNING"):
        await notifier.deliver([announcement()])

    assert "sUpErSeCrEt" not in caplog.text
    assert "hooks.slack.com" in caplog.text
    await notifier.aclose()


async def test_userinfo_credentials_are_never_logged(caplog):
    """A URL may carry user:password@, which netloc would have carried into the log."""
    settings = make_settings(notify_webhook_url="https://alice:hunter2@hooks.example.com/a/b")
    notifier = Notifier(settings)
    Webhook(status=500).attach(notifier)

    with caplog.at_level("WARNING"):
        await notifier.deliver([announcement()])

    assert "hunter2" not in caplog.text
    assert "alice" not in caplog.text
    assert "hooks.example.com" in caplog.text
    await notifier.aclose()
