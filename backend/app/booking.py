"""Booking creation.

The slot is never trusted from the client. At write time we rebuild the
available slots from the engine - same availability rules, same conflict set,
same minimum notice - and require the requested instant to be among them. That
closes the gap between "looked free a minute ago" and "is free now", and the
unique index is the final backstop against a double-book race.
"""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, notifications, zoom
from .availability import (
    EventTypeConfig, Interval, Schedule, WeeklyRule, DateOverride, generate_slots,
)
from .gcal import external_busy


class BookingError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


async def _load_context(conn, slug: str):
    et = await conn.fetchrow(
        "select * from sched.event_types where slug=$1 and active=true", slug
    )
    if et is None:
        raise BookingError(404, "Unknown or inactive event type")

    rules = await conn.fetch(
        "select wday, override_date, from_min, to_min, is_unavailable "
        "from sched.availability_rules where schedule_id=$1",
        et["availability_schedule_id"],
    )
    sched_row = await conn.fetchrow(
        "select timezone from sched.availability_schedules where id=$1",
        et["availability_schedule_id"],
    )

    weekly, overrides = [], []
    for r in rules:
        if r["wday"] is not None:
            weekly.append(WeeklyRule(r["wday"], r["from_min"], r["to_min"]))
        else:
            overrides.append(DateOverride(r["override_date"], r["from_min"],
                                          r["to_min"], r["is_unavailable"]))

    schedule = Schedule(timezone=sched_row["timezone"], weekly=weekly, overrides=overrides)
    cfg = EventTypeConfig(
        duration_min=et["duration_min"],
        buffer_before_min=et["buffer_before_min"],
        buffer_after_min=et["buffer_after_min"],
        min_notice_min=et["min_notice_min"],
        date_range_days=et["date_range_days"],
        slot_step_min=et["slot_step_min"],
    )
    return et, schedule, cfg


async def _busy_for_day(conn, host_date, tz_name) -> list[Interval]:
    """All scheduled bookings overlapping the host-local day (any event type:
    the host cannot be in two places). External free/busy is added on top."""
    tz = ZoneInfo(tz_name)
    day_start = datetime(host_date.year, host_date.month, host_date.day, tzinfo=tz).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    rows = await conn.fetch(
        """select start_utc, end_utc from sched.bookings
            where status='scheduled' and end_utc > $1 and start_utc < $2""",
        day_start, day_end,
    )
    busy = [Interval(r["start_utc"], r["end_utc"]) for r in rows]
    busy += await external_busy(day_start, day_end)
    return busy


async def create_booking(payload) -> dict:
    start = payload.start_utc
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            et, schedule, cfg = await _load_context(conn, payload.event_slug)

            host_date = start.astimezone(ZoneInfo(schedule.timezone)).date()
            busy = await _busy_for_day(conn, host_date, schedule.timezone)

            now = datetime.now(timezone.utc)
            valid = generate_slots(schedule, cfg, busy, now, only_date=host_date)
            if not any(abs((start - s).total_seconds()) < 1 for s in valid):
                raise BookingError(409, "That slot is no longer available")

            end = start + timedelta(minutes=et["duration_min"])

            # Participant list: the booker is always element 0, then guests.
            participants = [{"name": payload.name, "email": str(payload.email)}]
            for g in payload.guests:
                participants.append({"name": g.name,
                                     "email": str(g.email) if g.email else None})
            if len(participants) > et["max_invitees"]:
                raise BookingError(
                    422, f"This meeting allows up to {et['max_invitees']} participant(s)")

            join_url = await zoom.meeting_for_booking(
                et, start, et["duration_min"], participants
            )

            try:
                booking = await conn.fetchrow(
                    """insert into sched.bookings
                         (event_type_id, start_utc, end_utc, booker_name, booker_email,
                          booker_timezone, host_timezone, location_url, answers, participants)
                       values ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb)
                       returning *""",
                    et["id"], start, end, payload.name, str(payload.email),
                    payload.booker_timezone, schedule.timezone, join_url,
                    json.dumps(payload.answers or {}), json.dumps(participants),
                )
            except Exception as exc:  # unique_violation -> already taken
                if "bookings_no_double_book" in str(exc):
                    raise BookingError(409, "That slot was just taken")
                raise

            await notifications.queue_for_booking(conn, booking, et)

    return {
        "id": str(booking["id"]),
        "event": et["name"],
        "start_utc": booking["start_utc"].isoformat(),
        "end_utc": booking["end_utc"].isoformat(),
        "join_url": booking["location_url"],
        "participants": booking["participants"],
        "manage_token": str(booking["cancel_token"]),
        "booker_timezone": booking["booker_timezone"],
        "host_timezone": booking["host_timezone"],
    }
