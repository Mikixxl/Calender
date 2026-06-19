"""The single outbound queue.

On booking we enqueue a confirmation (now) plus one reminder per configured
offset (start minus offset). A no-show flips in its own nudge. The confirmation
is also sent synchronously at booking time (send_confirmation_sync), so the
booker has the Zoom link in hand the instant they finish - it never waits on the
tick. The tick worker, hit on a schedule, sends the rest (reminders, no-show,
cancellation) exactly once - the dedupe key guarantees no doubles.

Every time is rendered in the booker's zone, Berlin and UTC, so nothing is
ambiguous in an inbox.
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from . import db
from .availability import render_dual
from .config import settings
from .emailer import send_email

NAVY = "#0a1f44"
GOLD = "#c8a24b"


def _shell(title: str, body_html: str) -> str:
    return f"""<div style="font-family:Georgia,'Times New Roman',serif;max-width:580px;margin:0 auto;color:#1a1a1a;background:#ffffff">
  <div style="background:{NAVY};padding:22px 28px;border-bottom:3px solid {GOLD}">
    <div style="color:#fff;font-size:19px;letter-spacing:.5px;font-weight:600">International Finance Bank</div>
    <div style="color:{GOLD};font-size:12px;letter-spacing:1.5px;margin-top:2px">BANQUE FINANCIERE INTERNATIONALE</div>
  </div>
  <div style="padding:28px">
    <h2 style="color:{NAVY};font-size:21px;margin:0 0 16px;font-weight:600">{title}</h2>
    {body_html}
  </div>
  <div style="padding:16px 28px;border-top:1px solid #eee;color:#888;font-size:12px">
    International Finance Bank Ltd
  </div>
</div>"""


def _times_block(start_utc: datetime, end_utc: datetime, booker_tz: str, host_tz: str) -> str:
    s = render_dual(start_utc, booker_tz, host_tz)
    e = render_dual(end_utc, booker_tz, host_tz)
    return f"""<table style="border-collapse:collapse;margin:8px 0 20px;font-size:15px">
      <tr><td style="padding:4px 12px 4px 0;color:#888">Your time</td>
          <td style="padding:4px 0"><strong>{s['booker']['label']}</strong> &ndash; {e['booker']['label']}</td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#888">Berlin time</td>
          <td style="padding:4px 0">{s['host']['label']} &ndash; {e['host']['label']}</td></tr>
    </table>"""


def _btn(url: str, label: str, bg: str = NAVY) -> str:
    fg = "#241a00" if bg == GOLD else "#fff"
    return (f'<a href="{url}" style="display:inline-block;background:{bg};color:{fg};'
            f'text-decoration:none;padding:11px 20px;border-radius:6px;font-size:14px;'
            f'font-weight:600;margin:4px 8px 4px 0">{label}</a>')



def _coerce(v):
    """jsonb may arrive as a JSON string from legacy double-encoded rows; turn
    it back into a Python object so .get()/.items() are always safe."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:  # noqa: BLE001
            return v
    return v

def _participant_names(ctx: dict) -> str:
    ps = ctx.get("participants") or []
    return ", ".join(p.get("name", "") for p in ps if p.get("name"))


# --------------------------------------------------------------------------
# Zoom helpers - the meeting id and chat link are derived from the join URL,
# which is the only Zoom artefact we persist (bookings.location_url).
# --------------------------------------------------------------------------
def _zoom_meeting_id(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"/j/(\d+)", url) or re.search(r"/(?:my|s)/(\d+)", url)
    return m.group(1) if m else ""


def _fmt_meeting_id(mid: str) -> str:
    if not mid:
        return ""
    if len(mid) == 11:
        return f"{mid[0:3]} {mid[3:7]} {mid[7:11]}"
    if len(mid) == 10:
        return f"{mid[0:3]} {mid[3:6]} {mid[6:10]}"
    return mid


def _zoom_chat_link(url: str, mid: str) -> str:
    if not mid:
        return ""
    host = "us02web.zoom.us"
    m = re.match(r"https?://([^/]+)/", url or "")
    if m:
        host = m.group(1)
    return f"https://{host}/launch/jc/{mid}"


def _zoom_block(start_utc, end_utc, booker_tz, host_tz, join, event_name, names) -> str:
    """The premium Zoom invitation block: topic, time (local + Berlin + UTC),
    join, chat link, meeting id - styled to match a formal IFB invitation."""
    s = render_dual(start_utc, booker_tz, host_tz)
    e = render_dual(end_utc, booker_tz, host_tz)
    su = render_dual(start_utc, "UTC", "UTC")
    topic = f"{names} &middot; {event_name}" if names else event_name
    mid = _zoom_meeting_id(join)
    mid_fmt = _fmt_meeting_id(mid)
    chat = _zoom_chat_link(join, mid)

    time_rows = (
        f"<div style='margin:2px 0'><strong>{s['booker']['label']}</strong> &ndash; {e['booker']['label']}</div>"
    )
    if booker_tz != host_tz:
        time_rows += f"<div style='color:#666;font-size:13px;margin:2px 0'>Berlin: {s['host']['label']}</div>"
    time_rows += f"<div style='color:#666;font-size:13px;margin:2px 0'>UTC: {su['booker']['label']}</div>"

    rows = (
        f"<tr><td style='padding:6px 16px 6px 0;color:#888;vertical-align:top;white-space:nowrap'>Topic</td>"
        f"<td style='padding:6px 0'>{topic}</td></tr>"
        f"<tr><td style='padding:6px 16px 6px 0;color:#888;vertical-align:top'>Time</td>"
        f"<td style='padding:6px 0'>{time_rows}</td></tr>"
    )
    if mid_fmt:
        rows += (f"<tr><td style='padding:6px 16px 6px 0;color:#888;vertical-align:top'>Meeting&nbsp;ID</td>"
                 f"<td style='padding:6px 0;font-variant-numeric:tabular-nums'>{mid_fmt}</td></tr>")

    chat_line = (f"<p style='margin:6px 0 0;font-size:13px;color:#666'>Meeting chat link: "
                 f"<a href='{chat}' style='color:{NAVY}'>{chat}</a></p>") if chat else ""

    return (
        f"<div style='border:1px solid #e7e4dd;border-radius:8px;padding:18px 20px;margin:6px 0 20px;background:#faf9f6'>"
        f"<p style='margin:0 0 12px;color:{NAVY};font-weight:600'>International Finance Bank invites you to a scheduled Zoom meeting.</p>"
        f"<table style='border-collapse:collapse;font-size:15px;width:100%'>{rows}</table>"
        f"<p style='margin:14px 0 0'>{_btn(join, 'Join Zoom Meeting', GOLD)}</p>"
        f"{chat_line}"
        f"</div>"
    )


# --------------------------------------------------------------------------
# Message bodies
# --------------------------------------------------------------------------
def build_booker_message(ntype: str, ctx: dict) -> tuple[str, str, str]:
    times = _times_block(ctx["start_utc"], ctx["end_utc"], ctx["booker_tz"], ctx["host_tz"])
    join = ctx["location_url"] or ""
    manage = f"{settings.public_site_url}/manage?token={ctx['cancel_token']}"
    rebook = f"{settings.public_site_url}/{ctx['event_slug']}"
    name = ctx["booker_name"]
    ev = ctx["event_name"]
    names = _participant_names(ctx)

    if ntype == "confirmation":
        rescheduled = bool(ctx.get("rescheduled"))
        zoom = _zoom_block(ctx["start_utc"], ctx["end_utc"], ctx["booker_tz"],
                           ctx["host_tz"], join, ev, names)
        lead = ("Your meeting has been rescheduled. The new details are below."
                if rescheduled else
                f"Thank you for your booking for <strong>{ev}</strong>. Your meeting is confirmed.")
        body = (
            f"<p>Dear {name},</p>"
            f"<p>{lead}</p>"
            f"{zoom}"
            f"<p style='font-size:14px;color:#444'>Please be punctual. You will receive an email "
            f"reminder 30 minutes before the meeting begins.</p>"
            f"<p style='font-size:14px;color:#444'>If you are unable to attend, please reschedule to "
            f"a later time. You may reschedule at any point up to 15 minutes before the meeting.</p>"
            f"<p style='margin-top:18px'>{_btn(manage, 'Reschedule or cancel')}</p>"
        )
        title = "Your meeting has been rescheduled" if rescheduled else "Your meeting is confirmed"
        subject = (f"Rescheduled: {ev}" if rescheduled else f"Confirmed: {ev}")
        return (subject, _shell(title, body), "")

    if ntype == "reminder":
        zoom = _zoom_block(ctx["start_utc"], ctx["end_utc"], ctx["booker_tz"],
                           ctx["host_tz"], join, ev, names)
        body = (f"<p>Dear {name},</p><p>A reminder of your upcoming <strong>{ev}</strong>. "
                f"It begins in approximately 30 minutes.</p>"
                f"{zoom}"
                f"<p style='font-size:14px;color:#444'>If you can no longer attend, you may reschedule "
                f"up to 15 minutes before the meeting.</p>"
                f"<p style='margin-top:16px'>{_btn(manage, 'Reschedule or cancel')}</p>")
        return (f"Reminder: {ev}", _shell("Your meeting is coming up", body), "")

    if ntype == "no_show":
        body = (f"<p>Dear {name},</p><p>We had your <strong>{ev}</strong> in the diary but did "
                f"not see you join. No difficulty - if you would still like to speak, "
                f"choose a new time that suits you.</p>"
                f"<p style='margin-top:16px'>{_btn(rebook, 'Book another time', GOLD)}</p>")
        return ("We missed you - book another time", _shell("We missed you", body), "")

    if ntype == "cancellation":
        body = (f"<p>Dear {name},</p><p>Your <strong>{ev}</strong> has been cancelled.</p>{times}"
                f"<p>{_btn(rebook, 'Book a new time', GOLD)}</p>")
        return (f"Cancelled: {ev}", _shell("Your meeting was cancelled", body), "")

    return (f"{ev}", _shell(ev, f"<p>Dear {name},</p>{times}"), "")


def build_host_notice(ctx: dict) -> tuple[str, str, str]:
    """Sent to the host on confirmation: the details plus one-click marking
    that drives the no-show loop after the meeting."""
    times = _times_block(ctx["start_utc"], ctx["end_utc"], ctx["host_tz"], ctx["host_tz"])
    base = f"{settings.public_api_url}/internal/mark?auth={settings.admin_token}&booking={ctx['booking_id']}"
    answers = ctx.get("answers") or {}
    arows = "".join(
        f"<tr><td style='padding:3px 12px 3px 0;color:#888;vertical-align:top'>{k}</td>"
        f"<td style='padding:3px 0'>{v}</td></tr>"
        for k, v in answers.items()
    )
    atable = f"<table style='border-collapse:collapse;font-size:14px;margin:8px 0 16px'>{arows}</table>" if arows else ""
    join = ctx.get("location_url") or ""
    join_line = f"<p style='font-size:13px'>Join: <a href='{join}'>{join}</a></p>" if join else ""
    body = (f"<p>New booking: <strong>{ctx['event_name']}</strong></p>"
            f"<p><strong>Participants:</strong> {_participant_names(ctx)}</p>"
            f"<p>Contact: {ctx['booker_name']} &lt;{ctx['booker_email']}&gt; ({ctx['booker_tz']})</p>"
            f"{times}{join_line}{atable}"
            f"<p style='margin-top:16px;color:#888;font-size:13px'>After the meeting:</p>"
            f"<p>{_btn(base + '&status=completed', 'Mark attended', '#1a7f4b')}"
            f"{_btn(base + '&status=no_show', 'Mark no-show', '#a23a3a')}</p>")
    return (f"New booking: {ctx['event_name']} - {ctx['booker_name']}",
            _shell("New booking", body), "")


# --------------------------------------------------------------------------
# ctx from a booking row
# --------------------------------------------------------------------------
def _ctx_from_booking(booking, etype, *, rescheduled: bool = False) -> dict:
    return {
        "booking_id": booking["id"],
        "start_utc": booking["start_utc"], "end_utc": booking["end_utc"],
        "booker_name": booking["booker_name"], "booker_email": booking["booker_email"],
        "booker_tz": booking["booker_timezone"], "host_tz": booking["host_timezone"],
        "location_url": booking["location_url"], "cancel_token": booking["cancel_token"],
        "event_name": etype["name"], "event_slug": etype["slug"],
        "answers": _coerce(booking["answers"]), "participants": _coerce(booking["participants"]),
        "rescheduled": rescheduled,
    }


# --------------------------------------------------------------------------
# Synchronous confirmation - sent at booking time, independent of the tick.
# --------------------------------------------------------------------------
async def send_confirmation_sync(booking, etype, *, rescheduled: bool = False) -> bool:
    """Send the booker confirmation (and, on a first booking, the host notice)
    immediately. Marks any pending confirmation row 'sent' so the tick never
    sends it twice. Best-effort: returns True on success, False on failure -
    a failure leaves the queued row 'pending' for the tick to retry."""
    ctx = _ctx_from_booking(booking, etype, rescheduled=rescheduled)
    try:
        subject, html, text = build_booker_message("confirmation", ctx)
        await asyncio.to_thread(send_email, ctx["booker_email"], subject, html, text)
        if not rescheduled:
            try:
                hs, hh, ht = build_host_notice(ctx)
                await asyncio.to_thread(send_email, settings.gmail_user, hs, hh, ht)
            except Exception as exc:  # noqa: BLE001 - host copy must not block the booker copy
                print(f"[mail] host notice failed: {exc!r}")
            await db.execute(
                "update sched.notifications set status='sent', sent_at=now(), "
                "attempts=attempts+1 where booking_id=$1 and ntype='confirmation' "
                "and status='pending'",
                booking["id"],
            )
        return True
    except Exception as exc:  # noqa: BLE001 - never break a booking on mail
        print(f"[mail] sync confirmation failed for {booking['id']}: {exc!r}")
        return False


# --------------------------------------------------------------------------
# Enqueue
# --------------------------------------------------------------------------
async def queue_for_booking(conn, booking, etype) -> None:
    bid = booking["id"]
    start = booking["start_utc"]
    now = datetime.now(timezone.utc)

    # Confirmation, immediately. (Also sent synchronously; this row is the
    # audit trail and the fallback if the synchronous send fails.)
    await conn.execute(
        """insert into sched.notifications (booking_id, ntype, scheduled_for_utc, dedupe_key)
           values ($1,'confirmation',$2,$3) on conflict (dedupe_key) do nothing""",
        bid, now, f"{bid}:confirmation",
    )
    # Reminders, one per offset that still lies in the future.
    for off in (etype["reminder_offsets_min"] or []):
        when = start - timedelta(minutes=int(off))
        if when > now:
            await conn.execute(
                """insert into sched.notifications (booking_id, ntype, scheduled_for_utc, dedupe_key)
                   values ($1,'reminder',$2,$3) on conflict (dedupe_key) do nothing""",
                bid, when, f"{bid}:reminder:{int(off)}",
            )


async def requeue_reminders(conn, booking, etype) -> None:
    """After a reschedule: drop pending reminders tied to the old start and
    enqueue fresh ones for the new start. Confirmation rows are untouched."""
    bid = booking["id"]
    start = booking["start_utc"]
    now = datetime.now(timezone.utc)
    await conn.execute(
        "delete from sched.notifications where booking_id=$1 and ntype='reminder' and status='pending'",
        bid,
    )
    for off in (etype["reminder_offsets_min"] or []):
        when = start - timedelta(minutes=int(off))
        if when > now:
            await conn.execute(
                """insert into sched.notifications (booking_id, ntype, scheduled_for_utc, dedupe_key)
                   values ($1,'reminder',$2,$3)
                   on conflict (dedupe_key) do update set scheduled_for_utc=excluded.scheduled_for_utc,
                       status='pending', sent_at=null, attempts=0, error=null""",
                bid, when, f"{bid}:reminder:{int(off)}",
            )


async def queue_no_show(conn, booking_id) -> None:
    await conn.execute(
        """insert into sched.notifications (booking_id, ntype, scheduled_for_utc, dedupe_key)
           values ($1,'no_show',now(),$2) on conflict (dedupe_key) do nothing""",
        booking_id, f"{booking_id}:no_show",
    )


async def queue_cancellation(conn, booking_id) -> None:
    await conn.execute(
        """insert into sched.notifications (booking_id, ntype, scheduled_for_utc, dedupe_key)
           values ($1,'cancellation',now(),$2) on conflict (dedupe_key) do nothing""",
        booking_id, f"{booking_id}:cancellation",
    )


# --------------------------------------------------------------------------
# Tick: send everything due
# --------------------------------------------------------------------------
async def process_due(limit: int = 50) -> dict:
    rows = await db.fetch(
        """select n.id, n.ntype, b.id as booking_id, b.start_utc, b.end_utc,
                  b.booker_name, b.booker_email, b.booker_timezone, b.host_timezone,
                  b.location_url, b.cancel_token, b.answers, b.participants,
                  e.name as event_name, e.slug as event_slug
             from sched.notifications n
             join sched.bookings b   on b.id = n.booking_id
             join sched.event_types e on e.id = b.event_type_id
            where n.status = 'pending' and n.scheduled_for_utc <= now()
            order by n.scheduled_for_utc
            limit $1""",
        limit,
    )

    sent = failed = 0
    for r in rows:
        ctx = {
            "booking_id": r["booking_id"], "start_utc": r["start_utc"], "end_utc": r["end_utc"],
            "booker_name": r["booker_name"], "booker_email": r["booker_email"],
            "booker_tz": r["booker_timezone"], "host_tz": r["host_timezone"],
            "location_url": r["location_url"], "cancel_token": r["cancel_token"],
            "event_name": r["event_name"], "event_slug": r["event_slug"],
            "answers": _coerce(r["answers"]), "participants": _coerce(r["participants"]),
        }
        try:
            subject, html, text = build_booker_message(r["ntype"], ctx)
            await asyncio.to_thread(send_email, r["booker_email"], subject, html, text)
            # On confirmation, also notify the host with the marking links.
            if r["ntype"] == "confirmation":
                hs, hh, ht = build_host_notice(ctx)
                await asyncio.to_thread(send_email, settings.gmail_user, hs, hh, ht)
            await db.execute(
                "update sched.notifications set status='sent', sent_at=now(), attempts=attempts+1 where id=$1",
                r["id"],
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            await db.execute(
                "update sched.notifications set status='failed', attempts=attempts+1, error=$2 where id=$1",
                r["id"], str(exc)[:500],
            )
            failed += 1

    return {"processed": len(rows), "sent": sent, "failed": failed}
