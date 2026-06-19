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

from . import db, gcal, notifications, zoom
from .availability import (
    EventTypeConfig, Interval, Schedule, WeeklyRule, DateOverride, generate_slots,
)
from .gcal import external_busy
from .config import settings


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
        min_lead_days=et["min_lead_days"] if "min_lead_days" in et else 1,
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
            where (status='scheduled'
                   or (status='pending_payment' and pending_expires_at > now()))
              and end_utc > $1 and start_utc < $2""",
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
            # The free endpoint may never finalize a paid meeting. Paid types go
            # through the PayPal create-order / capture-order pair only.
            if et["is_paid"]:
                raise BookingError(402, "This meeting requires payment")

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

        # Best-effort calendar mirror, AFTER the booking is committed. Composio
        # is never in the critical path here: the row already exists, the Zoom
        # link is minted, the confirmation mail is queued. If this fails the
        # booking still stands; we only miss annotating it with the event id.
        try:
            names = [p["name"].strip() for p in participants
                     if isinstance(p, dict) and p.get("name") and p["name"].strip()]
            cal_summary = (", ".join(names) if names else et["name"])[:190]
            cal_desc = (f'{et["name"]}\n'
                        f'Booked by: {payload.name} <{payload.email}>\n'
                        f'Join: {booking["location_url"]}')
            ev_id = await gcal.create_event(
                cal_summary, booking["start_utc"], et["duration_min"],
                description=cal_desc, location=booking["location_url"],
            )
            if ev_id:
                await conn.execute(
                    "update sched.bookings set gcal_event_id=$1 where id=$2",
                    ev_id, booking["id"],
                )
        except Exception as exc:  # noqa: BLE001 - calendar mirror is best-effort
            print(f"[gcal] mirror create failed: {exc!r}")

        # Send the confirmation immediately - the booker has the Zoom link in
        # hand the moment they finish, independent of the tick. Best-effort:
        # send_confirmation_sync swallows its own errors and the queued row
        # stays as a fallback for the tick if the live send fails.
        try:
            await notifications.send_confirmation_sync(booking, et)
        except Exception as exc:  # noqa: BLE001
            print(f"[mail] create_booking sync send failed: {exc!r}")

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


# --------------------------------------------------------------------------
# Paid bookings: hold the slot, then finalize on a confirmed PayPal capture.
# --------------------------------------------------------------------------
async def create_pending_paid(payload) -> dict:
    """Reserve the slot for a paid event and return the row for a PayPal order.

    Mirrors create_booking's slot re-validation, but writes a pending_payment
    row that holds the slot for the payment window and mints no Zoom. The
    meeting is created only at capture, once PayPal confirms the money.
    """
    start = payload.start_utc
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            et, schedule, cfg = await _load_context(conn, payload.event_slug)
            if not et["is_paid"]:
                raise BookingError(400, "This event type is not a paid meeting")
            if not et["price_cents"] or et["price_cents"] <= 0:
                raise BookingError(409, "This meeting has no price set")

            host_date = start.astimezone(ZoneInfo(schedule.timezone)).date()
            busy = await _busy_for_day(conn, host_date, schedule.timezone)
            now = datetime.now(timezone.utc)
            valid = generate_slots(schedule, cfg, busy, now, only_date=host_date)
            if not any(abs((start - s).total_seconds()) < 1 for s in valid):
                raise BookingError(409, "That slot is no longer available")

            end = start + timedelta(minutes=et["duration_min"])

            participants = [{"name": payload.name, "email": str(payload.email)}]
            for g in payload.guests:
                participants.append({"name": g.name,
                                     "email": str(g.email) if g.email else None})
            if len(participants) > et["max_invitees"]:
                raise BookingError(
                    422, f"This meeting allows up to {et['max_invitees']} participant(s)")

            expires = now + timedelta(minutes=settings.paypal_pending_minutes)
            booking = await conn.fetchrow(
                """insert into sched.bookings
                     (event_type_id, start_utc, end_utc, status, booker_name,
                      booker_email, booker_timezone, host_timezone, answers,
                      participants, amount_cents, currency, pending_expires_at)
                   values ($1,$2,$3,'pending_payment',$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11,$12)
                   returning *""",
                et["id"], start, end, payload.name, str(payload.email),
                payload.booker_timezone, schedule.timezone,
                json.dumps(payload.answers or {}), json.dumps(participants),
                et["price_cents"], et["currency"] or settings.paypal_currency, expires,
            )
    return {"booking": booking, "et": et}


async def finalize_paid_booking(conn, booking_id, capture_id: str) -> dict:
    """Turn a captured pending_payment row into a real booking: conflict-check,
    mint Zoom, flip to scheduled, queue the confirmation. Runs inside the
    caller's transaction. The caller does the post-commit calendar mirror.
    """
    b = await conn.fetchrow(
        "select * from sched.bookings where id=$1 for update", booking_id
    )
    if b is None:
        raise BookingError(404, "Booking not found")
    if b["status"] == "scheduled":
        et = await conn.fetchrow(
            "select * from sched.event_types where id=$1", b["event_type_id"])
        return {"booking": b, "et": et}          # idempotent: already finalized
    if b["status"] != "pending_payment":
        raise BookingError(409, f"Booking is {b['status']}, cannot finalize")

    et = await conn.fetchrow(
        "select * from sched.event_types where id=$1", b["event_type_id"])

    clash = await conn.fetchrow(
        """select 1 from sched.bookings
            where event_type_id=$1 and start_utc=$2 and id<>$3
              and (status='scheduled'
                   or (status='pending_payment' and pending_expires_at > now()))
            limit 1""",
        b["event_type_id"], b["start_utc"], b["id"],
    )
    if clash is not None:
        raise BookingError(409, "slot_taken")

    participants = b["participants"] or []
    join_url = await zoom.meeting_for_booking(
        et, b["start_utc"], et["duration_min"], participants)

    booking = await conn.fetchrow(
        """update sched.bookings
              set status='scheduled', location_url=$2, paypal_capture_id=$3,
                  pending_expires_at=null
            where id=$1
            returning *""",
        b["id"], join_url, capture_id,
    )
    await notifications.queue_for_booking(conn, booking, et)
    return {"booking": booking, "et": et}


# --------------------------------------------------------------------------
# Reschedule: move a scheduled booking to a new time, up to 15 minutes before
# the current start. The Zoom link is kept; reminders are re-queued; the
# calendar mirror is moved; an updated confirmation goes out at once.
# --------------------------------------------------------------------------
async def reschedule_booking(token: str, new_start_utc) -> dict:
    start = new_start_utc
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            b = await conn.fetchrow(
                """select b.*, e.slug as event_slug from sched.bookings b
                     join sched.event_types e on e.id = b.event_type_id
                    where b.cancel_token=$1 for update""",
                token,
            )
            if b is None:
                raise BookingError(404, "Booking not found")
            if b["status"] != "scheduled":
                raise BookingError(409, "Only a scheduled booking can be rescheduled")
            # The cutoff is measured against the CURRENT start: no moves inside
            # the final 15 minutes.
            if now >= b["start_utc"] - timedelta(minutes=15):
                raise BookingError(
                    409, "It is too late to reschedule this meeting (within 15 minutes of the start)")

            et, schedule, cfg = await _load_context(conn, b["event_slug"])

            host_date = start.astimezone(ZoneInfo(schedule.timezone)).date()
            busy = await _busy_for_day(conn, host_date, schedule.timezone)
            valid = generate_slots(schedule, cfg, busy, now, only_date=host_date)
            if not any(abs((start - s).total_seconds()) < 1 for s in valid):
                raise BookingError(409, "That slot is no longer available")

            clash = await conn.fetchrow(
                """select 1 from sched.bookings
                    where event_type_id=$1 and start_utc=$2 and id<>$3
                      and (status='scheduled'
                           or (status='pending_payment' and pending_expires_at > now()))
                    limit 1""",
                b["event_type_id"], start, b["id"],
            )
            if clash is not None:
                raise BookingError(409, "That slot was just taken")

            end = start + timedelta(minutes=et["duration_min"])
            old_gcal = b["gcal_event_id"]
            try:
                booking = await conn.fetchrow(
                    "update sched.bookings set start_utc=$2, end_utc=$3 where id=$1 returning *",
                    b["id"], start, end,
                )
            except Exception as exc:  # unique_violation -> already taken
                if "bookings_no_double_book" in str(exc):
                    raise BookingError(409, "That slot was just taken")
                raise

            # Same Zoom link stays valid; refresh the reminder schedule.
            await notifications.requeue_reminders(conn, booking, et)

        # Post-commit, best-effort: move the calendar mirror.
        try:
            names = [p["name"].strip() for p in (booking["participants"] or [])
                     if isinstance(p, dict) and p.get("name") and p["name"].strip()]
            cal_summary = (", ".join(names) if names else et["name"])[:190]
            cal_desc = (f'{et["name"]}\n'
                        f'Booked by: {booking["booker_name"]} <{booking["booker_email"]}>\n'
                        f'Join: {booking["location_url"]}')
            new_id = await gcal.create_event(
                cal_summary, booking["start_utc"], et["duration_min"],
                description=cal_desc, location=booking["location_url"],
            )
            if new_id:
                await conn.execute(
                    "update sched.bookings set gcal_event_id=$1 where id=$2",
                    new_id, booking["id"],
                )
            if old_gcal:
                await gcal.delete_event(old_gcal)
        except Exception as exc:  # noqa: BLE001 - mirror move is best-effort
            print(f"[gcal] reschedule mirror failed: {exc!r}")

        # Updated confirmation, sent at once.
        try:
            await notifications.send_confirmation_sync(booking, et, rescheduled=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[mail] reschedule confirmation failed: {exc!r}")

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
