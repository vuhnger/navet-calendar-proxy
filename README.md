# navet-calendar-proxy

Exposes the public data behind [ifinavet.no](https://ifinavet.no/events) as
subscribable iCalendar feeds and as a read-only JSON API, so it can be pulled
into other student platforms.

### → API documentation: **<https://navet.vuhnger.dev/docs>**

Browsable, with every endpoint, parameter and response shape. Generated from the
code, so it cannot drift from what the service actually does.

## The feeds

```
https://navet.vuhnger.dev/calendar.ics        Navet's events
https://navet.vuhnger.dev/registrations.ics   when registration opens
https://navet.vuhnger.dev/jobs.ics            job application deadlines
```

Paste any of them into a calendar client, or into Peoply's **ICS-URL** field
under organisation settings. They refresh themselves hourly — nothing needs to
be re-pasted when Navet adds an event.

`/calendar.ics` is the one to subscribe to if you only want one. The other two
are separate documents on purpose: an importer like Peoply turns every `VEVENT`
into a platform event, so folding registration openings into the main feed would
make every event show up twice.

## Getting told about new job listings

Two ways, and they complement each other.

`https://navet.vuhnger.dev/jobs.xml` is an Atom feed of listings, newest posting
first. Nothing to configure, and being pull-based it cannot miss anything.

For Slack or Discord, point the service at incoming webhooks and each refresh
posts one message per record. An incoming webhook is bound to a single channel,
so the two kinds get a URL each:

```
NOTIFY_JOBS_WEBHOOK_URL=…          # job ads
NOTIFY_REGISTRATION_WEBHOOK_URL=…  # bedpres registration openings
```

`NOTIFY_WEBHOOK_URL` is a fallback for any kind without its own, so setting only
that sends both to the same place. Each kind is delivered independently: a dead
job-ads webhook does not stop the bedpres channel from being told anything.

The messages are one per new listing:

```
Ny stillingsannonse fra Bekk
Sommerjobb 2027
Søk her: https://…
```

and one per registration that has just opened:

```
Påmelding åpen for Netcompany (https://ifinavet.no/events/…)
```

The format is picked from the URL's host, so a Slack webhook gets `text` and a
Discord one gets `content` with no further configuration. That URL is a
credential — its path is what identifies the channel — so it lives in the
server's env file and only its host is ever logged.

None of this polls: the hourly refresh already has the data, so "new" is a set
difference against the previous one and costs no extra upstream requests. The
tradeoff is that a listing can be up to an hour old before it is announced.

Most of the design here is about not flooding the channel. The first run adopts
everything currently published without announcing it, so switching the webhook
on does not replay a semester of history, and the same applies if the state file
is ever lost or unreadable. The record of what has been announced is only
pruned for a kind that actually produced something, because job listings are
fetched best-effort: a failed query looks exactly like "there are none", and
forgetting on that would make the next healthy refresh announce every listing
again. `NOTIFY_MAX_ITEMS` caps a single refresh on top of all that.

Delivery is best-effort: a record counts as announced whether or not the webhook
accepted it, because a webhook that returns after a day of downtime should not
then dump everything it missed into the channel. That is what the Atom feed is
for.

## The API

| | |
| --- | --- |
| [`/docs`](https://navet.vuhnger.dev/docs) | Swagger UI. Browsable, and you can call the endpoints from the page. |
| [`/redoc`](https://navet.vuhnger.dev/redoc) | The same thing in a reading-oriented layout. |
| [`/openapi.json`](https://navet.vuhnger.dev/openapi.json) | The machine-readable schema both are rendered from. |

All three are generated from the code at runtime, so there is deliberately no
endpoint table in this README. One would be a second description of the same
thing, maintained by hand, and it would be wrong within a month.

Everything under `/api` returns JSON, including fields the calendar format has
nowhere to put: registration times, seat counts, organizers, company logos, the
original HTML alongside the plain-text conversion. All list endpoints page with
`limit`/`offset`.

## How it gets the data

ifinavet.no is a Next.js frontend over a **public Convex backend**, and its
event, company and job-listing queries are public Convex functions. This service
calls those directly:

```
POST https://gallant-pheasant-518.convex.cloud/api/query
{"path": "events/queries:getAll", "args": {"semester": "høst", "year": 2026}, "format": "json"}
```

It does **not** scrape HTML. That is deliberate — the rendered page sits behind
Vercel bot protection (a plain `curl` gets a "Vercel Security Checkpoint" page,
not the events), and markup changes would break a scraper silently. The Convex
queries return typed records and are the same source the website itself renders
from.

The queries live in `packages/backend/convex/` in
[ifinavet/yggdrasil](https://github.com/ifinavet/yggdrasil):

| Query | Used for |
| --- | --- |
| `events/queries:getPossibleSemesters` | Which semesters to sweep |
| `events/queries:getAll` | The events themselves, per semester |
| `events/queries:getEvent` | Organizers. One call per event — see below |
| `companies/queries:getAll` | The company register |
| `companies/queries:getById` | Resolving a company's logo to a storage URL |
| `jobListings/queries:getAll` | Job listings, with the company logo already resolved |

Only records with `published: true` are included, for both events and job
listings.

Requests never reach upstream: every response is served from what the background
refresh already built. That keeps response times independent of Convex, and
keeps our call volume against somebody else's backend proportional to time
rather than to our traffic.

## Design decisions worth knowing

**Events have no end time upstream.** The Convex schema stores only `eventStart`.
Every `VEVENT` therefore gets `DTEND = DTSTART + DEFAULT_DURATION_MINUTES`
(default 2 h). Change it with the env var if that turns out wrong.

**The feed is never allowed to be empty.** It keeps `PAST_DAYS` (default 180) of
history. This is not for the benefit of readers — it is because Peoply archives
every previously imported event whose UID is missing from the current fetch, so a
momentarily empty feed would wipe the imported events. For the same reason the
service returns `503` rather than an empty calendar when it has no data at all,
and keeps serving the last good feed when upstream is down.

**A refresh that loses most of the feed is rejected.** An outage is the easy case:
it raises, and the last good feed keeps being served. The dangerous case is a
*successful* upstream response that is empty or truncated — schema drift, a
renamed field, a backend bug that stops setting `published`. That would pass every
structural check and quietly archive everything downstream. So a refresh that
shrinks the feed below `MIN_EVENT_RATIO` (default 50%) of the previous one is
treated as breakage, not as news: it is rejected, `last_error` is set, and the
previous feed stays live. Growth is always allowed, and the very first fetch may
legitimately be empty.

**UIDs are the Convex document id** (`<id>@ifinavet.no`), which is stable across
refreshes, so subscribers update events in place instead of duplicating them.
The derived feeds suffix theirs (`-registration`, `-deadline`) so that
subscribing to more than one in the same calendar does not merge entries.

**Enrichment is best-effort; events are not.** Companies, job listings, logos and
organizers each cost extra upstream calls, and a failure in any of them is
logged and skipped rather than raised. Only the event fetch failing fails the
refresh, because that is the part subscribers depend on. Each can be switched
off independently if Navet's backend ever needs the quiet.

**Logo lookups are cached on something that changes when the logo does.** The
company register does not resolve storage URLs, so an unseen logo costs one
`getById` plus a `HEAD` to learn its media type. Both results are cached — the
URL on the company's logo id, the media type on the URL itself — so a company
swapping its logo is re-resolved while the steady state costs nothing. Logos in
formats consumers cannot render (SVG, mostly) are reported as absent rather than
linked, since pointing a client at an image it will not draw is worse than
pointing it at nothing.

**Organizer e-mail addresses are not published by default.** Upstream exposes
them, but they are personal addresses of Navet volunteers rather than role
accounts, so republishing them on a public endpoint is a deliberate choice:
`INCLUDE_ORGANIZER_EMAILS=true`. Names and roles are always included. The
calendar feeds never carry the addresses at all.

**Times are emitted as UTC (`...Z`).** No `VTIMEZONE` block is needed and every
client resolves the same instant. `X-WR-TIMEZONE: Europe/Oslo` is set for display.

**`SUMMARY` is capped at 150 chars and `LOCATION` at 100.** Peoply stores them in
`varchar(150)` / `varchar(100)` columns, and an oversize value fails the whole
sync rather than just that field.

**`DESCRIPTION` is plain text.** Upstream descriptions are HTML; they are
converted with a stdlib `HTMLParser` (not a regex) so malformed markup cannot
produce surprising output. Character entities are decoded, because Peoply strips
tags but does not decode entities.

## Operating it

Hosted on NREC at `158.37.66.4` (`ssh exp`), behind nginx with a Let's Encrypt
certificate that renews automatically via `certbot.timer`.

```bash
ssh exp
sudo systemctl status navet-ics          # service state
sudo journalctl -u navet-ics -f          # logs
curl -s localhost:8000/api/status | jq   # is the data fresh, and what did the last refresh do?
```

`/api/status` always answers `200`, including when the data is stale — it is the
endpoint for looking at the problem. `/readyz` reports the same thing but fails
with `503` instead, which is what the deploy script waits on.

The service runs as the unprivileged `navet-ics` user under a hardened systemd
unit, bound to `127.0.0.1:8000`; nginx is the only public listener. The last good
dataset is persisted to `/var/lib/navet-ics/dataset.json` and all three feeds are
rebuilt from it on startup, so a restart never serves an empty calendar. A
pre-existing `calendar.ics` from an older release is still read as a fallback.

Configuration lives in `/etc/navet-ics/navet-ics.env`. Every setting has a
working default — see [`.env.example`](.env.example) for the full list.

## Deploying

Pushing to `main` runs the checks and, if they pass, deploys automatically
(`.github/workflows/`). CI rsyncs the repo to a staging directory on the server
and invokes one root-owned script, `/usr/local/bin/navet-ics-deploy`, which the
deploy account may run via a single-command sudo rule and cannot itself modify.
That script snapshots the current release, installs the new one, restarts, waits
for `/readyz`, and **rolls back automatically** if readiness is never reached.

First-time provisioning of a fresh host:

```bash
sudo ./deploy/install.sh navet.vuhnger.dev
sudo certbot --nginx -d navet.vuhnger.dev --redirect --agree-tos -m <email> --no-eff-email
```

## Development

```bash
uv sync           # create .venv from uv.lock
uv run pytest     # tests
uv run ruff check # lint
uv run uvicorn navet_ics.app:app --reload
```

Dependencies are locked in `uv.lock`; `uv sync --frozen` gives a byte-identical
environment in CI and on the server.
