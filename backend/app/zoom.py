"""Zoom integration seam.

Pilot behaviour mirrors the live Calendly exactly: every booking uses the one
static room configured on the event type, no Zoom API call. This single
function is the swap point. When we wire real per-booking meetings, this is the
only place that changes: create a meeting on the connected Zoom account, set the
topic to the event name, the start time and duration from the booking, register
each participant by name, and return the unique join URL.
"""
from .availability import Interval  # noqa: F401  (kept for type parity)


async def meeting_for_booking(event, start_utc, duration_min, participants) -> str:
    """Return the join URL for a booking.

    Today: the static room on the event type. Later: a unique Zoom meeting per
    booking carrying every participant's name. Signature already passes start,
    duration and participants so the swap needs no caller change.
    """
    return event["location_url"]
