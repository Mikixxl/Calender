import os, sys, time, asyncio
from datetime import timedelta, timezone
import requests, asyncpg

COMPOSIO_BASE = "https://backend.composio.dev/api/v3"
LIST_TOOL = "GOOGLECALENDAR_EVENTS_LIST"
DEL_TOOL  = "GOOGLECALENDAR_DELETE_EVENT"
GCAL_CONNECTION = "ca_ezzmrSYp4xxn"
GCAL_USER = "scheduler-private"
GCAL_CALENDAR_ID = "primary"
TITLE_PREFIX = "Kein Booking"
HTTP_TIMEOUT = 90
DRY = os.environ.get("DEDUP_DRY", "").strip() not in ("", "0", "false", "False")

def execute(tool, args, api_key):
    r = requests.post(
        f"{COMPOSIO_BASE}/tools/execute/{tool}",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={"connected_account_id": GCAL_CONNECTION, "user_id": GCAL_USER, "arguments": args},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"{tool} HTTP {r.status_code}: {r.text[:300]}")
    body = r.json()
    if body.get("successful") is False:
        raise RuntimeError(f"{tool} failed: {body.get('error')}")
    return body.get("data") or {}

def list_events(api_key, tmin, tmax):
    out, page, pages = [], None, 0
    while True:
        args = {"calendarId": GCAL_CALENDAR_ID, "timeMin": tmin, "timeMax": tmax,
                "q": TITLE_PREFIX, "singleEvents": True, "orderBy": "startTime",
                "maxResults": 2500}
        if page:
            args["pageToken"] = page
        d = execute(LIST_TOOL, args, api_key)
        rd = d.get("response_data") or d
        items = rd.get("items") or d.get("items") or []
        if pages == 0 and not items:
            print("DEBUG list keys:", list(d.keys()), "| rd keys:", list(rd.keys())[:12])
        out.extend(items)
        page = rd.get("nextPageToken") or d.get("nextPageToken")
        pages += 1
        if not page or pages > 20:
            break
    return out

async def main():
    api_key = os.environ["COMPOSIO_API_KEY"]
    db_url = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        keep = {r["gcal_event_id"] for r in await conn.fetch(
            "select gcal_event_id from sched.hebcal_events where gcal_event_id is not null")}
        bmin = await conn.fetchval("select min(start_utc) from sched.busy_blocks where calendar_key='hebcal'")
        bmax = await conn.fetchval("select max(end_utc) from sched.busy_blocks where calendar_key='hebcal'")
    finally:
        await conn.close()
    tmin = (bmin - timedelta(days=2)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmax = (bmax + timedelta(days=2)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"DEDUP dry={DRY}  window {tmin}..{tmax}  ledger_keep={len(keep)}")
    evs = list_events(api_key, tmin, tmax)
    ours = [e for e in evs
            if (e.get("summary") or "").startswith(TITLE_PREFIX)
            and e.get("status") != "cancelled"]
    print(f"listed total={len(evs)}  ours(Kein Booking)={len(ours)}")
    groups = {}
    for e in ours:
        st = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date")
        groups.setdefault((e.get("summary"), st), []).append(e)
    to_delete = []
    dup_groups = 0
    for key, grp in groups.items():
        if len(grp) <= 1:
            continue
        dup_groups += 1
        keeper = next((e for e in grp if e.get("id") in keep), None) or \
                 sorted(grp, key=lambda x: x.get("id", ""))[0]
        for e in grp:
            if e.get("id") != keeper.get("id"):
                to_delete.append((key[1], key[0], e.get("id"), keeper.get("id") in keep))
    print(f"unique spans on calendar={len(groups)}  duplicate_groups={dup_groups}  events_to_delete={len(to_delete)}")
    for st, summ, eid, keeper_ledgered in to_delete:
        print(f"  DUP {st} | {summ} | del {eid} | keeper_in_ledger={keeper_ledgered}")
    if DRY:
        print("DRY RUN - nothing deleted")
        return
    deleted, failed = 0, 0
    for st, summ, eid, _ in to_delete:
        try:
            execute(DEL_TOOL, {"event_id": eid, "calendar_id": GCAL_CALENDAR_ID,
                               "send_updates": "none"}, api_key)
            deleted += 1
            time.sleep(0.35)
        except Exception as exc:
            failed += 1
            print(f"  delete fail {eid}: {exc}", file=sys.stderr)
    print(f"RESULT deleted={deleted} failed={failed}")

if __name__ == "__main__":
    asyncio.run(main())
