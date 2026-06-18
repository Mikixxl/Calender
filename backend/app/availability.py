"""
Slot engine for the IFB scheduler.

Design rule, non-negotiable: every instant is computed and stored in UTC.
The host's working hours are wall-clock rules that live in an IANA zone
(Europe/Berlin). For each individual date we build the wall-clock boundary
*with* that zone attached, so the UTC offset is resolved per date and DST
shifts handle themselves. A 09:00 Berlin start is 08:00 UTC in winter and
07:00 UTC in summer, computed, never hard-coded.

The engine is pure: no database, no network. It takes plain dataclasses in
and returns a list of UTC datetimes out. That is what makes it testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Interval:
    """A half-open busy interval [start, end) in UTC."""
    start: datetime
    end: datetime


@dataclass(frozen=True)
class WeeklyRule:
    # wday encoding matches the database: 0=Sun, 1=Mon, ... 6=Sat.
    wday: int
    from_min: int          # minutes from local midnight, inclusive
    to_min: int            # minutes from local midnight, exclusive


@dataclass(frozen=True)
class DateOverride:
    on: date
    from_min: int | None = None
    to_min: int | None = None
    unavailable: bool = False


@dataclass
class Schedule:
    timezone: str                              # IANA name, e.g. 'Europe/Berlin'
    weekly: list[WeeklyRule] = field(default_factory=list)
    overrides: list[DateOverride] = field(default_factory=list)


@dataclass
class EventTypeConfig:
    duration_min: int
    buffer_before_min: int = 0
    buffer_after_min: int = 0
    min_notice_min: int = 0
    date_range_days: int = 60
    slot_step_min: int = 30


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _wallclock(d: date, minutes: int, tz: ZoneInfo) -> datetime:
    """Return the UTC instant for `minutes` past local midnight on date `d`.

    The wall-clock time is constructed with the zone attached, which is what
    makes zoneinfo resolve the correct offset for that specific date. We do
    NOT add a timedelta to local midnight; that would do absolute-time math
    and drift by an hour across a DST boundary.
    """
    extra_days, mins = divmod(minutes, 1440)
    target = d + timedelta(days=extra_days)
    hh, mm = divmod(mins, 60)
    local = datetime(target.year, target.month, target.day, hh, mm, tzinfo=tz)
    return local.astimezone(timezone.utc)


def _overlaps(a_start: datetime, a_end: datetime, busy: list[Interval]) -> bool:
    for b in busy:
        if a_start < b.end and b.start < a_end:
            return True
    return False


def _wday_of(d: date) -> int:
    # isoweekday(): Mon=1 .. Sun=7 ; % 7 gives Sun=0, Mon=1 .. Sat=6.
    return d.isoweekday() % 7


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
def generate_slots(
    schedule: Schedule,
    etype: EventTypeConfig,
    busy: list[Interval],
    now_utc: datetime,
    horizon_days: int | None = None,
    only_date: date | None = None,
) -> list[datetime]:
    """Generate bookable start instants (UTC), ascending.

    busy        : existing commitments in UTC (own bookings + external free/busy)
    now_utc     : current instant, tz-aware UTC
    horizon_days: override the event type's date_range_days (handy for tests)
    only_date   : restrict to a single host-local date (handy for tests)
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware UTC")

    tz = ZoneInfo(schedule.timezone)
    earliest = now_utc + timedelta(minutes=etype.min_notice_min)
    horizon = etype.date_range_days if horizon_days is None else horizon_days

    overrides = {o.on: o for o in schedule.overrides}
    weekly: dict[int, list[tuple[int, int]]] = {}
    for r in schedule.weekly:
        weekly.setdefault(r.wday, []).append((r.from_min, r.to_min))

    start_day = now_utc.astimezone(tz).date()
    last_day = start_day + timedelta(days=horizon)

    dur = timedelta(minutes=etype.duration_min)
    step = timedelta(minutes=etype.slot_step_min)
    bb = timedelta(minutes=etype.buffer_before_min)
    ba = timedelta(minutes=etype.buffer_after_min)

    slots: list[datetime] = []
    d = start_day
    while d <= last_day:
        if only_date is not None and d != only_date:
            d += timedelta(days=1)
            continue

        ov = overrides.get(d)
        if ov is not None:
            if ov.unavailable or ov.from_min is None or ov.to_min is None:
                d += timedelta(days=1)
                continue
            intervals = [(ov.from_min, ov.to_min)]
        else:
            intervals = weekly.get(_wday_of(d), [])

        for fmin, tmin in intervals:
            win_start = _wallclock(d, fmin, tz)
            win_end = _wallclock(d, tmin, tz)
            cur = win_start
            while cur + dur <= win_end:
                if cur >= earliest:
                    block_start = cur - bb
                    block_end = cur + dur + ba
                    if not _overlaps(block_start, block_end, busy):
                        slots.append(cur)
                cur += step
        d += timedelta(days=1)

    slots.sort()
    return slots


# --------------------------------------------------------------------------
# Presentation helper (dual-timezone rendering)
# --------------------------------------------------------------------------
def render_dual(instant_utc: datetime, booker_tz: str, host_tz: str) -> dict:
    """Render one UTC instant in both the booker's zone and the host's zone.

    Every confirmation, reminder and no-show email uses this so the time is
    never ambiguous.
    """
    b = instant_utc.astimezone(ZoneInfo(booker_tz))
    h = instant_utc.astimezone(ZoneInfo(host_tz))
    return {
        "utc": instant_utc.isoformat(),
        "booker": {
            "tz": booker_tz,
            "iso": b.isoformat(),
            "label": b.strftime("%a %d %b %Y, %H:%M %Z"),
        },
        "host": {
            "tz": host_tz,
            "iso": h.isoformat(),
            "label": h.strftime("%a %d %b %Y, %H:%M %Z"),
        },
    }
