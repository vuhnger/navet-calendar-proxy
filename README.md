# navet-calendar-proxy

Exposes the events from [ifinavet.no](https://ifinavet.no/events) as a subscribable
iCalendar feed, so they can be pulled into other student platforms.

## The feed

```
https://navet.vuhnger.dev/calendar.ics
```

Paste that into any calendar client or into Peoply's **ICS-URL** field under
organisation settings. It refreshes itself hourly — nothing needs to be re-pasted
when Navet adds an event.

| Endpoint | Purpose |
| --- | --- |
| `GET /calendar.ics` | The feed. `text/calendar; charset=utf-8`, supports `ETag`/`If-None-Match`. |
| `GET /healthz` | Liveness. `200` whenever the process is up. |
| `GET /readyz` | Readiness. `200` only if a feed exists and upstream data is fresh; `503` otherwise. Includes `last_error`, `last_success`, and the event count. |
| `GET /` | Redirects to `/calendar.ics`. |

## How it gets the data

ifinavet.no is a Next.js frontend over a **public Convex backend**, and its event
queries are public Convex functions. This service calls those directly:

```
POST https://gallant-pheasant-518.convex.cloud/api/query
{"path": "events/queries:getAll", "args": {"semester": "høst", "year": 2026}, "format": "json"}
```

It does **not** scrape HTML. That is deliberate — the rendered page sits behind
Vercel bot protection (a plain `curl` gets a "Vercel Security Checkpoint" page,
not the events), and markup changes would break a scraper silently. The Convex
queries return typed records and are the same source the website itself renders
from.

Event data comes from `packages/backend/convex/events/queries.ts` in
[ifinavet/yggdrasil](https://github.com/ifinavet/yggdrasil). Only events with
`published: true` are included.

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
sudo systemctl status navet-ics      # service state
sudo journalctl -u navet-ics -f      # logs
curl -s localhost:8000/readyz | jq   # is the data fresh?
```

The service runs as the unprivileged `navet-ics` user under a hardened systemd
unit, bound to `127.0.0.1:8000`; nginx is the only public listener. The last good
feed is persisted to `/var/lib/navet-ics/calendar.ics` so a restart never serves
an empty calendar.

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
