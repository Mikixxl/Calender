"""External free/busy source.

Reads pre-synced busy intervals from sched.busy_blocks. A GitHub Actions cron
pulls each connected Google Calendar's free/busy via Composio and writes the
busy intervals into that table; the live booking path only ever reads here, so
a booking never depends on Composio or Google being reachable at request time.
If the table is empty (sync never ran, or every calendar is free), this returns
nothing and the engine still blocks against the scheduler's own bookings.
"""
from datetime import datetime

from . import db
from .availability import Interval


async def external_busy(start_utc: datetime, end_utc: datetime) -> list[Interval]:
    rows = await db.fetch(
        """select start_utc, end_utc from sched.busy_blocks
            where end_utc > $1 and start_utc < $2""",
        start_utc, end_utc,
    )
    return [Interval(r["start_utc"], r["end_utc"]) for r in rows]
