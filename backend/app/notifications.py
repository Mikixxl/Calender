"""The single outbound queue.

On booking we enqueue a confirmation (now) plus one reminder per configured
offset (start minus offset). A no-show flips in its own nudge. The tick worker,
hit every few minutes by cron-job.org, sends everything due exactly once - the
dedupe key guarantees no doubles even if a tick overlaps or retries.

Every time is rendered in both the booker's zone and Berlin, so nothing is
ambiguous in an inbox.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from . import db
from .availability import render_dual
from .config import settings
from .emailer import send_email

NAVY = "#0a1f44"
GOLD = "#c8a24b"


def _shell(title: str, body_html: str) -> str:
    return f"""<div style="font-family:Georgia,'Times New Roman',serif;max-width:560px;margin:0 auto;color:#1a1a1a">
  <div style="background:{NAVY};padding:20px 24px;border-bottom:3px solid {GOLD}">
    <div style="color:#fff;font-size:18px;letter-spacing:.5px">IFB Bank</div>
  </div>
  <div style="padding:24px">
    <h2 style="color:{NAVY};font-size:20px;margin:0 0 16px">{title}</h2>
    {body_html}
  </div>
  <div style="padding:16px 24px;border-top:1px solid #eee;color:#888;font-size:12px">
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
    return (f'<a href="{url}" style="display:inline-block;background:{bg};color:#fff;'
            f'text-decoration:none;padding:10px 18px;border-radius:4px;font-size:14px;'
            f'margin:4px 8px 4px 0">{label}</a>')


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

    if ntype == "confirmation":
        body = (f"<p>Dear {name},</p><p>Your <strong>{ev}</strong> is confirmed.</p>"
                f"{times}"
                f"<p>Join link:</p><p>{_btn(join, 'Join the Zoom meeting', GOLD)}</p>"
                f"<p style='margin-top:20px'>Need to change it? "
                f"{_btn(manage, 'Reschedule or cancel')}</p>")
        return (f"Confirmed: {ev}", _shell("Your meeting is confirmed", body), "")

    if ntype == "reminder":
        body = (f"<p>Dear {name},</p><p>A reminder of your upcoming <strong>{ev}</strong>.</p>"
                f"{times}"
                f"<p>{_btn(join, 'Join the Zoom meeting', GOLD)}</p>"
                f"<p style='margin-top:20px'>{_btn(manage, 'Reschedule or cancel')}</p>")
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
    body = (f"<p>New booking: <strong>{ctx['event_name']}</strong></p>"
            f"<p>{ctx['booker_name']} &lt;{ctx['booker_email']}&gt; ({ctx['booker_tz']})</p>"
            f"{times}{atable}"
            f"<p style='margin-top:16px;color:#888;font-size:13px'>After the meeting:</p>"
            f"<p>{_btn(base + '&status=completed', 'Mark attended', '#1a7f4b')}"
            f"{_btn(base + '&status=no_show', 'Mark no-show', '#a23a3a')}</p>")
    return (f"New booking: {ctx['event_name']} - {ctx['booker_name']}",
            _shell("New booking", body), "")


# --------------------------------------------------------------------------
# Enqueue
# --------------------------------------------------------------------------
async def queue_for_booking(conn, booking, etype) -> None:
    bid = booking["id"]
    start = booking["start_utc"]
    now = datetime.now(timezone.utc)

    # Confirmation, immediately.
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
                  b.location_url, b.cancel_token, b.answers,
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
            "answers": r["answers"],
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
