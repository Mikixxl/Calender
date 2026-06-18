"""External free/busy source.

The live system already funnels every meeting into one Google Calendar, and
that calendar is the single source of truth for conflicts. Until the service
account credentials are wired, this returns no external busy intervals, so the
engine still blocks against the scheduler's own confirmed bookings (handled in
booking.py) and never self-double-books. Wiring this in is a later step, not a
correctness gap for the pilot.
"""
from datetime import datetime

from .availability import Interval


async def external_busy(start_utc: datetime, end_utc: datetime) -> list[Interval]:
    # TODO: call Google Calendar freebusy.query for the host calendar over the
    # window and map each busy block to an Interval. Credentials arrive via a
    # service account in env; no secret lives in the repo.
    return []
