"""
Self-test for the slot engine. No framework, just asserts and a PASS line.
Run: python3 backend/app/test_availability.py
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from availability import (
    Schedule, WeeklyRule, DateOverride, EventTypeConfig, Interval,
    generate_slots, render_dual,
)

BERLIN = "Europe/Berlin"

# The live "Working hours" schedule: Sun 10-22, Mon-Thu 09-22, Fri 09-16, Sat closed.
SCHED = Schedule(
    timezone=BERLIN,
    weekly=[
        WeeklyRule(0, 600, 1320),   # Sun 10:00-22:00
        WeeklyRule(1, 540, 1320),   # Mon 09:00-22:00
        WeeklyRule(2, 540, 1320),   # Tue
        WeeklyRule(3, 540, 1320),   # Wed
        WeeklyRule(4, 540, 1320),   # Thu
        WeeklyRule(5, 540, 960),    # Fri 09:00-16:00
    ],
)

ET = EventTypeConfig(duration_min=30, slot_step_min=30, min_notice_min=0,
                     date_range_days=0)


def first_wednesday(y: int, m: int) -> date:
    d = date(y, m, 1)
    while d.isoweekday() != 3:  # Wednesday
        d += timedelta(days=1)
    return d


def at(d: date) -> datetime:
    """now_utc fixed at 00:00 UTC on date d, so the whole host day is in range."""
    return datetime(d.year, d.month, d.day, 0, 0, tzinfo=timezone.utc)


fails = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


# ---- 1. DST: same wall-clock start, different UTC in winter vs summer -------
jan_wed = first_wednesday(2026, 1)
jul_wed = first_wednesday(2026, 7)

jan = generate_slots(SCHED, ET, [], at(jan_wed), only_date=jan_wed)
jul = generate_slots(SCHED, ET, [], at(jul_wed), only_date=jul_wed)

jan_first_berlin = jan[0].astimezone(ZoneInfo(BERLIN))
jul_first_berlin = jul[0].astimezone(ZoneInfo(BERLIN))

print("DST winter/summer:")
check(f"winter first slot is 09:00 Berlin (got {jan_first_berlin:%H:%M})",
      (jan_first_berlin.hour, jan_first_berlin.minute) == (9, 0))
check(f"summer first slot is 09:00 Berlin (got {jul_first_berlin:%H:%M})",
      (jul_first_berlin.hour, jul_first_berlin.minute) == (9, 0))
check(f"winter 09:00 Berlin == 08:00 UTC (got {jan[0]:%H:%M}Z)", jan[0].hour == 8)
check(f"summer 09:00 Berlin == 07:00 UTC (got {jul[0]:%H:%M}Z)", jul[0].hour == 7)
check("winter and summer differ by one hour in UTC",
      jan[0].hour - jul[0].hour == 1)

# ---- 2. Saturday is dark (Shabbat) -----------------------------------------
sat = date(2026, 1, 10)               # a Saturday
while sat.isoweekday() != 6:
    sat += timedelta(days=1)
sat_slots = generate_slots(SCHED, ET, [], at(sat), only_date=sat)
print("Shabbat:")
check("Saturday yields zero slots", len(sat_slots) == 0)

# ---- 3. Friday closes early (last meeting ends by 16:00 Berlin) -------------
fri = date(2026, 1, 9)
while fri.isoweekday() != 5:
    fri += timedelta(days=1)
fri_slots = generate_slots(SCHED, ET, [], at(fri), only_date=fri)
last_end_berlin = (fri_slots[-1] + timedelta(minutes=30)).astimezone(ZoneInfo(BERLIN))
print("Friday short close:")
check(f"Friday last meeting ends by 16:00 Berlin (got {last_end_berlin:%H:%M})",
      (last_end_berlin.hour, last_end_berlin.minute) <= (16, 0))

# ---- 4. A busy interval removes exactly the overlapping slot ----------------
busy_start = jan[2]                    # block the 3rd slot
busy = [Interval(busy_start, busy_start + timedelta(minutes=30))]
jan_busy = generate_slots(SCHED, ET, busy, at(jan_wed), only_date=jan_wed)
print("Conflict subtraction:")
check("blocked slot disappears", busy_start not in jan_busy)
check("exactly one slot removed", len(jan_busy) == len(jan) - 1)

# ---- 5. min_notice pushes the earliest slot forward ------------------------
ET_notice = EventTypeConfig(duration_min=30, slot_step_min=30,
                            min_notice_min=240, date_range_days=0)
# now = 09:00 UTC on the winter Wednesday => +4h notice => earliest 13:00 UTC
now_mid = datetime(jan_wed.year, jan_wed.month, jan_wed.day, 9, 0, tzinfo=timezone.utc)
notice_slots = generate_slots(SCHED, ET_notice, [], now_mid, only_date=jan_wed)
print("Minimum notice:")
check(f"no slot earlier than now+4h (earliest {notice_slots[0]:%H:%M}Z)",
      all(s >= now_mid + timedelta(minutes=240) for s in notice_slots))

# ---- 6. Dual-timezone rendering --------------------------------------------
dual = render_dual(jan[0], "America/New_York", BERLIN)
print("Dual rendering (winter 09:00 Berlin):")
print("    booker:", dual["booker"]["label"])
print("    host:  ", dual["host"]["label"])
check("New York shows 03:00 for 09:00 Berlin in winter",
      dual["booker"]["iso"].startswith(f"{jan_wed.isoformat()}T03:00"))

print()
if fails:
    print(f"RESULT: {len(fails)} FAILED -> {fails}")
    raise SystemExit(1)
print("RESULT: ALL GREEN")
