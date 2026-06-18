"""External free/busy source, and the calendar write-back.

Read path: pre-synced busy intervals from sched.busy_blocks (a GitHub Actions
cron pulls each connected Google Calendar's free/busy via Composio). The live
booking path only ever reads here, so a booking never depends on Composio or
Google being reachable at request time.

Write path: create_event / delete_event mirror a confirmed booking into the
host's Google Calendar via Composio. These run AFTER the booking is committed
and are treated as best-effort by the caller - a Composio or Google outage
leaves the booking intact and merely skips the calendar entry.
"""
from datetime import datetime, timezone

import httpx

from . import db
from .availability import Interval
from .config import settings

_COMPOSIO_BASE = "https://backend.composio.dev/api/v3"
_GCAL_TIMEOUT = httpx.Timeout(15.0)


async def external_busy(start_utc: datetime, end_utc: datetime) -> list[Interval]:
    rows = await db.fetch(
        """select start_utc, end_utc from sched.busy_blocks
            where end_utc > $1 and start_utc < $2""",
        start_utc, end_utc,
    )
    return [Interval(r["start_utc"], r["end_utc"]) for r in rows]


# --------------------------------------------------------------------------
# Write-back: mirror a booking into the host's Google Calendar
# --------------------------------------------------------------------------
def _gcal_enabled() -> bool:
    return bool(
        settings.composio_api_key
        and settings.composio_gcal_connection
        and settings.composio_gcal_user
    )


async def _gcal_exec(tool: str, arguments: dict) -> dict:
    payload = {
        "connected_account_id": settings.composio_gcal_connection,
        "user_id": settings.composio_gcal_user,
        "arguments": arguments,
    }
    async with httpx.AsyncClient(timeout=_GCAL_TIMEOUT) as c:
        r = await c.post(
            f"{_COMPOSIO_BASE}/tools/execute/{tool}",
            headers={"x-api-key": settings.composio_api_key, "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"composio gcal {tool} {r.status_code}: {r.text[:300]}")
        data = r.json()
    if data.get("successful") is False:
        raise RuntimeError(f"composio gcal {tool} failed: {data.get('error')}")
    return data


async def create_event(summary, start_utc, duration_min,
                       description=None, location=None) -> str | None:
    """Create a calendar event for a booking; return its event id.

    Returns None when the mirror is not configured. Raises on a live API
    failure so the caller can log it - the caller treats any failure here as
    non-fatal, because the booking is already committed.
    """
    if not _gcal_enabled():
        return None
    dmin = int(duration_min)
    args = {
        "calendar_id": settings.gcal_calendar_id,
        "summary": summary,
        "start_datetime": start_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "timezone": "UTC",
        "event_duration_hour": dmin // 60,
        "event_duration_minutes": dmin % 60,
        "create_meeting_room": False,   # the Zoom link is the meeting
    }
    if description:
        args["description"] = description
    if location:
        args["location"] = location
    data = await _gcal_exec("GOOGLECALENDAR_CREATE_EVENT", args)
    d = data.get("data") or {}
    rd = d.get("response_data") or d
    return rd.get("id") or d.get("id")


async def delete_event(event_id: str) -> None:
    """Remove a mirrored event on cancellation. No-op if unconfigured or empty."""
    if not (_gcal_enabled() and event_id):
        return
    await _gcal_exec(
        "GOOGLECALENDAR_DELETE_EVENT",
        {"event_id": event_id, "calendar_id": settings.gcal_calendar_id},
    )
