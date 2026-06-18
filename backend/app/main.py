"""FastAPI surface for the scheduler.

Public:   list event types, fetch one, list slots in a booker timezone, book,
          view/cancel a booking by its manage token.
Internal: the cron tick that sends due mail, and the host's one-click
          attendance marking that drives the no-show nudge.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from . import db, notifications
from .availability import (
    EventTypeConfig, Schedule, WeeklyRule, DateOverride, Interval,
    generate_slots, render_dual,
)
from .booking import BookingError, create_booking, _load_context, _busy_for_day
from .gcal import external_busy
from .config import settings
from .models import BookingCreate

app = FastAPI(title="IFB Scheduler", version="0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.cors_origins == "*" else
        [o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/event-types")
async def list_event_types():
    rows = await db.fetch(
        """select slug, name, description_html, duration_min, kind, color,
                  is_paid, position
             from sched.event_types where active=true order by position"""
    )
    return [dict(r) for r in rows]


@app.get("/api/event-types/{slug}")
async def get_event_type(slug: str):
    et = await db.fetchrow(
        "select * from sched.event_types where slug=$1 and active=true", slug
    )
    if et is None:
        raise HTTPException(404, "Unknown event type")
    qs = await db.fetch(
        """select position, label, qtype, required, answer_choices, include_other
             from sched.event_type_questions
            where event_type_id=$1 and enabled=true order by position""",
        et["id"],
    )
    return {
        "slug": et["slug"], "name": et["name"],
        "description_html": et["description_html"],
        "duration_min": et["duration_min"], "kind": et["kind"],
        "max_invitees": et["max_invitees"],
        "color": et["color"], "is_paid": et["is_paid"],
        "questions": [dict(q) for q in qs],
    }


@app.get("/api/event-types/{slug}/slots")
async def list_slots(
    slug: str,
    from_: str = Query(alias="from"),
    to: str = Query(...),
    tz: str = Query(...),
):
    try:
        from_date = date.fromisoformat(from_)
        to_date = date.fromisoformat(to)
        ZoneInfo(tz)  # validate
    except Exception:
        raise HTTPException(400, "Bad from/to/tz")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        try:
            et, schedule, cfg = await _load_context(conn, slug)
        except BookingError as e:
            raise HTTPException(e.status, e.message)

        now = datetime.now(timezone.utc)
        host_today = now.astimezone(ZoneInfo(schedule.timezone)).date()
        horizon = min((to_date - host_today).days + 1, cfg.date_range_days)
        if horizon < 0:
            return {"event": et["name"], "timezone": tz, "days": {}}

        # Busy set across the whole visible window.
        win_start = datetime(from_date.year, from_date.month, from_date.day,
                             tzinfo=ZoneInfo(schedule.timezone)).astimezone(timezone.utc)
        win_end = (datetime(to_date.year, to_date.month, to_date.day,
                            tzinfo=ZoneInfo(schedule.timezone)) + timedelta(days=1)
                   ).astimezone(timezone.utc)
        rows = await conn.fetch(
            """select start_utc, end_utc from sched.bookings
                where status='scheduled' and end_utc > $1 and start_utc < $2""",
            win_start, win_end,
        )
        busy = [Interval(r["start_utc"], r["end_utc"]) for r in rows]
        busy += await external_busy(win_start, win_end)

        slots = generate_slots(schedule, cfg, busy, now, horizon_days=max(horizon, 0))

    days: dict[str, list] = {}
    for s in slots:
        bl = s.astimezone(ZoneInfo(tz)).date()
        if from_date <= bl <= to_date:
            days.setdefault(bl.isoformat(), []).append(
                render_dual(s, tz, schedule.timezone)
            )
    return {"event": et["name"], "timezone": tz,
            "host_timezone": schedule.timezone, "days": days}


@app.post("/api/bookings")
async def post_booking(payload: BookingCreate):
    try:
        return await create_booking(payload)
    except BookingError as e:
        raise HTTPException(e.status, e.message)


@app.get("/api/bookings/{token}")
async def get_booking(token: str):
    b = await db.fetchrow(
        """select b.*, e.name as event_name, e.slug as event_slug
             from sched.bookings b join sched.event_types e on e.id=b.event_type_id
            where b.cancel_token=$1""",
        token,
    )
    if b is None:
        raise HTTPException(404, "Not found")
    return {
        "event": b["event_name"], "status": b["status"],
        "start_utc": b["start_utc"].isoformat(),
        "booker_timezone": b["booker_timezone"], "host_timezone": b["host_timezone"],
        "participants": b["participants"],
        "times": render_dual(b["start_utc"], b["booker_timezone"], b["host_timezone"]),
        "join_url": b["location_url"],
        "rebook_url": f"{settings.public_site_url}/{b['event_slug']}",
    }


@app.post("/api/bookings/{token}/cancel")
async def cancel_booking(token: str):
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            b = await conn.fetchrow(
                "select id, status from sched.bookings where cancel_token=$1", token
            )
            if b is None:
                raise HTTPException(404, "Not found")
            if b["status"] != "scheduled":
                return {"ok": True, "status": b["status"]}
            await conn.execute(
                "update sched.bookings set status='canceled' where id=$1", b["id"]
            )
            await notifications.queue_cancellation(conn, b["id"])
    return {"ok": True, "status": "canceled"}


# -------------------------------------------------------------------------
# Internal
# -------------------------------------------------------------------------
@app.post("/internal/tick")
async def tick(token: str = Query(...)):
    if not settings.tick_token or token != settings.tick_token:
        raise HTTPException(403, "Forbidden")
    return await notifications.process_due()


@app.get("/internal/mark", response_class=HTMLResponse)
async def mark(auth: str = Query(...), booking: str = Query(...), status: str = Query(...)):
    if not settings.admin_token or auth != settings.admin_token:
        raise HTTPException(403, "Forbidden")
    if status not in ("completed", "no_show"):
        raise HTTPException(400, "status must be completed or no_show")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            b = await conn.fetchrow(
                "select id, status from sched.bookings where id=$1", booking
            )
            if b is None:
                raise HTTPException(404, "Not found")
            await conn.execute(
                "update sched.bookings set status=$2, attendance_marked_at=now() where id=$1",
                b["id"], status,
            )
            if status == "no_show":
                await notifications.queue_no_show(conn, b["id"])

    msg = ("Marked attended." if status == "completed"
           else "Marked no-show. A rebooking invitation is on its way.")
    return f"<div style='font-family:Georgia,serif;padding:40px;color:#0a1f44'><h2>{msg}</h2></div>"
