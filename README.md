# IFB Scheduler

A self-hosted booking system - our own Calendly. FastAPI on Fly.io (Frankfurt),
Postgres on Supabase (schema `sched`), a static booking page on Netlify, and a
cron-job.org tick that drives reminders. No per-booking Zoom API: every meeting
uses the one static Zoom room, exactly as the live account already does.

## Why it exists

Cost, control, branding, and one stack we own end to end. The timezone bug that
started this is solved at the root here, not patched.

## The timezone discipline

Every instant is stored and computed in UTC. The host's working hours are
wall-clock rules in an IANA zone (`Europe/Berlin`). For each individual date the
engine builds the wall-clock boundary with the zone attached, so the UTC offset
is resolved per date and DST handles itself: 09:00 Berlin is 08:00 UTC in winter
and 07:00 UTC in summer, computed, never hard-coded. Slot generation runs in UTC,
which sidesteps spring-forward gaps and fall-back doubles. The booker's zone is
auto-detected in the browser and overridable. Every email shows both the booker's
local time and Berlin, each labelled. See `backend/app/availability.py` and its
self-test `test_availability.py` (run it: `python3 test_availability.py`).

## Availability

The seeded schedule mirrors production: Sun 10-22, Mon-Thu 09-22, Fri 09-16,
Saturday closed. The short Friday and dark Saturday are deliberate and preserved.

## Reminders and no-shows

One queue table, `sched.notifications`, drives all outbound mail. On booking a
confirmation is queued immediately and one reminder per offset (default 24h and
1h, configurable per event type). The tick worker sends what is due; a dedupe key
prevents doubles. After a meeting the host clicks attended or no-show from the
booking email; on no-show the booker gets a single "we missed you" mail with a
rebooking link.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/api/event-types` | active event types |
| GET  | `/api/event-types/{slug}` | one event type + intake questions |
| GET  | `/api/event-types/{slug}/slots?from&to&tz` | slots, rendered in a booker zone |
| POST | `/api/bookings` | create a booking (slot revalidated server-side) |
| GET  | `/api/bookings/{token}` | view a booking by its manage token |
| POST | `/api/bookings/{token}/cancel` | cancel |
| POST | `/internal/tick?token=` | send due notifications (cron-job.org) |
| GET  | `/internal/mark?auth=&booking=&status=` | host marks attended / no-show |

## Environment (set as Fly secrets, never committed)

```
DATABASE_URL          postgres connection string (Supabase, owner role)
HOST_TIMEZONE         Europe/Berlin
GMAIL_USER            admin@intfiba.com
GMAIL_APP_PASSWORD    app password for the mailbox
MAIL_FROM_NAME        IFB Bank
TICK_TOKEN            shared secret guarding /internal/tick
ADMIN_TOKEN           shared secret guarding /internal/mark
PUBLIC_API_URL        https://ifb-scheduler.fly.dev
PUBLIC_SITE_URL       https://book.ifcifb.com
CORS_ORIGINS          https://book.ifcifb.com
```

## Deploy

Backend deploys to Fly on push to `main` via `.github/workflows/deploy.yml`
(needs `FLY_API_TOKEN` as a GitHub Actions secret). The tick is a cron-job.org
job hitting `/internal/tick?token=...` every few minutes - cron-job.org, not
GitHub Actions, because a 1-hour reminder cannot tolerate the scheduler lag.

## Database

`db/schema.sql` and `db/seed_pilot.sql` bootstrap a clean project. The pilot
lives in schema `sched` inside the shared Supabase project; it lifts to its own
project at production cutover. RLS is on with no public policies; the backend
reaches the tables as the owner role.

## Status

Pilot: slot engine proven, backend wired, one event type seeded
(Initial Prospective Client, 30 min). Next: the Netlify booking page, the Google
free/busy wiring, Fly deploy, the cron tick, then the remaining twelve event types.
