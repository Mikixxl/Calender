#!/usr/bin/env python3
"""Block Shabbat and Yom Tov in the IFB Scheduler from Hebcal data.

Two independent write paths, both driven by the same computed spans:

  1. ENFORCEMENT - sched.busy_blocks, calendar_key='hebcal'.
     Full-replaced every run across the whole horizon (default 18 months),
     so a Shabbat far in the future is already blocked the moment a slot for
     it could be requested. The live booking backend reads this table only,
     so enforcement never depends on Google or Composio being reachable at
     request time. sync_busy.py only ever touches calendar_key in
     ('private','bank'), so the 'hebcal' rows are never clobbered.

  2. VISIBILITY - busy events in mikixxl1@gmail.com's Google Calendar via the
     same Composio connection the scheduler already uses. Idempotent through
     the sched.hebcal_events ledger (one row per span, keyed by the span's
     start date), so a daily re-run never duplicates events.

A span = [candle-lighting - LEAD, havdalah]. Pairing each candle-lighting with
the next havdalah yields Shabbat and every Yom Tov (Rosh Hashana, Yom Kippur,
Pesach, Shavuot, Sukkot, Shmini Atzeret) automatically, and naturally leaves
working holidays (Chanukah, Purim) bookable. LEAD shifts the start earlier than
candle-lighting (default 60 min) per the "-1 hour" requirement.

Env:
  DATABASE_URL          (required)
  COMPOSIO_API_KEY      (required unless HEBCAL_DRY_RUN)
  HEBCAL_DRY_RUN=1      compute + print only, no DB or calendar writes
  HEBCAL_MONTHS=18      horizon length in months (approx, 30-day months)
  HEBCAL_LEAD_MIN=60    minutes to start the block before candle-lighting
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import requests

# --- Hebcal -----------------------------------------------------------------
HEBCAL_URL = "https://www.hebcal.com/hebcal"
GEONAMEID = 2950159  # Berlin, Germany (GeoNames)
HTTP_TIMEOUT = 90

# --- Composio / Google Calendar (mirror of sync_busy.py "private" entry) ----
COMPOSIO_BASE = "https://backend.composio.dev/api/v3"
CREATE_TOOL = "GOOGLECALENDAR_CREATE_EVENT"
GCAL_CONNECTION = "ca_ezzmrSYp4xxn"
GCAL_USER = "scheduler-private"
GCAL_CALENDAR_ID = "primary"  # = mikixxl1@gmail.com primary on that connection

CALENDAR_KEY = "hebcal"
TITLE_PREFIX = "Kein Booking"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def fetch_hebcal(start_date: str, end_date: str) -> list[dict]:
    """Full Jewish-calendar feed for Berlin with candle-lighting + havdalah."""
    params = {
        "v": 1,
        "cfg": "json",
        "geo": "geoname",
        "geonameid": GEONAMEID,
        "start": start_date,
        "end": end_date,
        "c": "on",      # candle lighting
        "b": 18,        # standard candle-lighting minutes before sunset
        "M": "on",      # havdalah at nightfall (tzeit)
        "maj": "on",    # major holidays (for naming + yom tov candles)
        "min": "off",
        "mod": "off",
        "nx": "off",
        "mf": "off",
        "ss": "off",
        "s": "off",     # no parashat clutter
        "leyning": "off",
    }
    r = requests.get(HEBCAL_URL, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json().get("items", []) or []


def _parse_dt(value: str) -> datetime | None:
    """Parse a timed Hebcal ISO date (with tz offset) to UTC, else None."""
    if not value or "T" not in value:
        return None  # all-day items (no time) are not candle/havdalah moments
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


def compute_spans(items: list[dict], lead_min: int) -> list[dict]:
    """Pair each candle-lighting with the next havdalah -> merged busy spans."""
    candles = []  # (utc_dt, memo)
    havdalah = []  # utc_dt
    holidays = []  # (date_str, title) for yomtov naming

    for it in items:
        cat = it.get("category")
        if cat == "candles":
            dt = _parse_dt(it.get("date", ""))
            if dt:
                candles.append((dt, (it.get("memo") or "").strip()))
        elif cat == "havdalah":
            dt = _parse_dt(it.get("date", ""))
            if dt:
                havdalah.append(dt)
        elif cat == "holiday" and it.get("yomtov"):
            holidays.append((str(it.get("date", ""))[:10], it.get("title", "")))

    candles.sort(key=lambda x: x[0])
    havdalah.sort()

    raw = []  # (start_utc, end_utc, name)
    lead = timedelta(minutes=lead_min)
    for c_dt, memo in candles:
        end = next((h for h in havdalah if h > c_dt), None)
        if end is None:
            continue  # havdalah beyond the fetched window; skip this edge span
        start = c_dt - lead
        if end <= start:
            continue
        name = memo  # erev-chag candles carry the holiday name in memo
        raw.append((start, end, name))

    raw.sort(key=lambda x: x[0])

    # merge overlapping/touching spans, collecting names
    merged: list[dict] = []
    for start, end, name in raw:
        if merged and start <= merged[-1]["end"]:
            cur = merged[-1]
            cur["end"] = max(cur["end"], end)
            if name:
                cur["names"].add(name)
        else:
            merged.append({"start": start, "end": end,
                           "names": {name} if name else set()})

    # attach yom-tov holiday titles that fall inside each span, build label
    for span in merged:
        s_date = span["start"].date()
        e_date = span["end"].date()
        for d_str, title in holidays:
            try:
                d = datetime.fromisoformat(d_str).date()
            except ValueError:
                continue
            if s_date <= d <= e_date and title:
                span["names"].add(title)
        label = " / ".join(sorted(span["names"])) if span["names"] else "Shabbat"
        if len(label) > 80:
            label = label[:77] + "..."
        span["title"] = f"{TITLE_PREFIX} - {label}"
        span["span_key"] = span["start"].astimezone(timezone.utc).date().isoformat()
    return merged


# --- Google Calendar via Composio -------------------------------------------
def gcal_create(api_key: str, span: dict) -> str | None:
    start = span["start"].astimezone(timezone.utc)
    dmin = int((span["end"] - span["start"]).total_seconds() // 60)
    args = {
        "calendar_id": GCAL_CALENDAR_ID,
        "summary": span["title"],
        "start_datetime": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "timezone": "UTC",
        "event_duration_hour": dmin // 60,
        "event_duration_minutes": dmin % 60,
        "create_meeting_room": False,
        "description": ("Automatisch aus Hebcal (Shabbat / Feiertag, Berlin). "
                        "Buchungen sind in diesem Zeitraum blockiert."),
    }
    r = requests.post(
        f"{COMPOSIO_BASE}/tools/execute/{CREATE_TOOL}",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={"connected_account_id": GCAL_CONNECTION,
              "user_id": GCAL_USER, "arguments": args},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"composio {CREATE_TOOL} {r.status_code}: {r.text[:300]}")
    body = r.json()
    if body.get("successful") is False:
        raise RuntimeError(f"composio {CREATE_TOOL} failed: {body.get('error')}")
    d = body.get("data") or {}
    rd = d.get("response_data") or d
    return rd.get("id") or d.get("id")


async def main() -> None:
    dry = os.environ.get("HEBCAL_DRY_RUN", "").strip() not in ("", "0", "false", "False")
    months = _env_int("HEBCAL_MONTHS", 18)
    lead_min = _env_int("HEBCAL_LEAD_MIN", 60)

    today = datetime.now(timezone.utc).date()
    start_date = today.isoformat()
    end_date = (today + timedelta(days=months * 30)).isoformat()

    print(f"hebcal: Berlin {start_date}..{end_date}  lead={lead_min}min  dry_run={dry}")
    items = fetch_hebcal(start_date, end_date)
    spans = compute_spans(items, lead_min)
    print(f"computed {len(spans)} busy spans (Shabbat + Yom Tov)")
    for s in spans[:6]:
        print(f"  {s['start']:%Y-%m-%d %H:%M}Z -> {s['end']:%H:%M}Z  {s['title']}")
    if len(spans) > 6:
        print(f"  ... (+{len(spans) - 6} more)")

    if not spans:
        print("no spans computed - aborting without writes", file=sys.stderr)
        sys.exit(1)

    if dry:
        print("DRY RUN - no DB or calendar writes")
        return

    db_url = os.environ["DATABASE_URL"]
    api_key = os.environ["COMPOSIO_API_KEY"]
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        # 1) ENFORCEMENT: full-replace the hebcal busy_blocks
        async with conn.transaction():
            await conn.execute(
                "delete from sched.busy_blocks where calendar_key=$1", CALENDAR_KEY
            )
            await conn.executemany(
                """insert into sched.busy_blocks (calendar_key, start_utc, end_utc)
                   values ($1, $2, $3)""",
                [(CALENDAR_KEY, s["start"], s["end"]) for s in spans],
            )
        print(f"busy_blocks: replaced calendar_key={CALENDAR_KEY} with {len(spans)} rows")

        # 2) VISIBILITY: create missing calendar events (idempotent via ledger)
        existing = {
            r["span_key"]
            for r in await conn.fetch("select span_key from sched.hebcal_events")
        }
        created = skipped = failed = 0
        first_err = None
        for span in spans:
            if span["span_key"] in existing:
                skipped += 1
                continue
            try:
                ev_id = gcal_create(api_key, span)
                await conn.execute(
                    """insert into sched.hebcal_events
                           (span_key, gcal_event_id, start_utc, end_utc, title)
                       values ($1, $2, $3, $4, $5)
                       on conflict (span_key) do nothing""",
                    span["span_key"], ev_id, span["start"], span["end"], span["title"],
                )
                created += 1
            except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
                failed += 1
                if first_err is None:
                    first_err = f"{type(exc).__name__}: {exc}"[:500]
                print(f"  [gcal/ledger fail] {span['span_key']}: {exc}", file=sys.stderr)
        print(f"gcal events: created={created} skipped={skipped} failed={failed}")
        if first_err:
            print(f"first_err: {first_err}", file=sys.stderr)
        try:
            await conn.execute(
                "insert into sched.hebcal_diag (spans, created, skipped, failed, note) "
                "values ($1,$2,$3,$4,$5)",
                len(spans), created, skipped, failed, first_err,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"diag write failed: {exc}", file=sys.stderr)
    finally:
        await conn.close()
    print("hebcal block sync complete")


if __name__ == "__main__":
    asyncio.run(main())
