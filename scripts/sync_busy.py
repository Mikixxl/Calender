#!/usr/bin/env python3
"""Sync Google Calendar busy intervals into sched.busy_blocks.

Runs in GitHub Actions on a cron. For each connected calendar it pulls
free/busy from Composio and replaces that calendar's rows in one transaction.
A per-calendar failure (e.g. a connection not yet authorized) is logged and
skipped, leaving that calendar's existing rows untouched, so one bad calendar
never blanks the other. The live booking backend only ever reads this table.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import requests

COMPOSIO_BASE = "https://backend.composio.dev/api/v3"
FREEBUSY_TOOL = "GOOGLECALENDAR_FIND_FREE_SLOTS"
HORIZON_DAYS = 60
TIMEZONE = "Europe/Berlin"

# (calendar_key, connected_account_id, user_id, calendar_id)
CALENDARS = [
    ("private", "ca_ezzmrSYp4xxn", "scheduler-private", "mikixxl1@gmail.com"),
    ("bank",    "ca_m6IfG5uriyA3", "scheduler-bank",    "admin@intfiba.com"),
]


def fetch_busy(api_key, conn_id, user_id, calendar_id, time_min, time_max):
    """Return [(start_utc, end_utc), ...] for one calendar, or raise."""
    r = requests.post(
        f"{COMPOSIO_BASE}/tools/execute/{FREEBUSY_TOOL}",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={
            "connected_account_id": conn_id,
            "user_id": user_id,
            "arguments": {
                "items": [calendar_id],
                "time_min": time_min,
                "time_max": time_max,
                "timezone": TIMEZONE,
            },
        },
        timeout=90,
    )
    r.raise_for_status()
    body = r.json()
    if not body.get("successful", False):
        raise RuntimeError(f"tool reported failure: {body.get('error')}")
    data = body.get("data") or {}
    cals = data.get("calendars") or {}
    info = cals.get(calendar_id) or {}
    out = []
    for b in info.get("busy", []):
        s = datetime.fromisoformat(b["start"]).astimezone(timezone.utc)
        e = datetime.fromisoformat(b["end"]).astimezone(timezone.utc)
        if e > s:
            out.append((s, e))
    return out


async def main():
    api_key = os.environ["COMPOSIO_API_KEY"]
    db_url = os.environ["DATABASE_URL"]

    now = datetime.now(timezone.utc)
    time_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(days=HORIZON_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        total = 0
        ok = 0
        for cal_key, conn_id, user_id, cal_id in CALENDARS:
            try:
                blocks = fetch_busy(api_key, conn_id, user_id, cal_id, time_min, time_max)
            except Exception as e:
                print(f"[skip] {cal_key} ({cal_id}): {e}", file=sys.stderr)
                continue
            async with conn.transaction():
                await conn.execute(
                    "delete from sched.busy_blocks where calendar_key=$1", cal_key
                )
                if blocks:
                    await conn.executemany(
                        """insert into sched.busy_blocks
                               (calendar_key, start_utc, end_utc)
                           values ($1, $2, $3)""",
                        [(cal_key, s, e) for s, e in blocks],
                    )
            ok += 1
            total += len(blocks)
            print(f"[ok] {cal_key} ({cal_id}): {len(blocks)} busy blocks")
        print(f"sync complete: {total} blocks across {ok}/{len(CALENDARS)} calendars")
        if ok == 0:
            sys.exit(1)  # every calendar failed -> surface as a failed run
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
