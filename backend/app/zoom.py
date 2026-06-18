"""Zoom integration: three independent ways to mint a per-booking meeting.

Order of attempt:
  1. Direct Zoom Server-to-Server OAuth REST - self-contained, no third party
     in the path at booking time. This is the primary.
  2. Composio ZOOM_CREATE_A_MEETING - a second, independent credential to the
     same Zoom account. Covers the case where the S2S app is mis-set or revoked.
  3. The static room baked on the event type - the floor that always works as
     long as Zoom itself is up.

A booking never fails because a meeting could not be minted. Every layer is
wrapped; the static link is the last resort. The May Composio blackout could
not have touched a booking with this in place: path 1 needs no Composio at all.
"""
import base64
import time
from datetime import timezone

import httpx

from .config import settings

_ZOOM_OAUTH = "https://zoom.us/oauth/token"
_ZOOM_API = "https://api.zoom.us/v2"
_COMPOSIO_EXEC = "https://backend.composio.dev/api/v3/tools/execute/ZOOM_CREATE_A_MEETING"

_TIMEOUT = httpx.Timeout(12.0)

# Cached S2S access token. The token's life is one hour; we reuse it across
# bookings and refresh a minute before expiry rather than minting one per call.
_token_cache = {"tok": "", "exp": 0.0}


def _iso_z(start_utc) -> str:
    """booking.py hands us a tz-aware UTC datetime; render Zoom's UTC literal."""
    return start_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_join_url(obj):
    """Walk an arbitrarily nested payload for the first join_url string."""
    if isinstance(obj, dict):
        if isinstance(obj.get("join_url"), str):
            return obj["join_url"]
        for v in obj.values():
            found = _find_join_url(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_join_url(v)
            if found:
                return found
    return None


async def _s2s_token() -> str:
    now = time.monotonic()
    if _token_cache["tok"] and _token_cache["exp"] - 60 > now:
        return _token_cache["tok"]
    if not (settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret):
        raise RuntimeError("zoom s2s not configured")
    basic = base64.b64encode(
        f"{settings.zoom_client_id}:{settings.zoom_client_secret}".encode()
    ).decode()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            _ZOOM_OAUTH,
            params={"grant_type": "account_credentials", "account_id": settings.zoom_account_id},
            headers={"Authorization": f"Basic {basic}"},
        )
        r.raise_for_status()
        data = r.json()
    _token_cache["tok"] = data["access_token"]
    _token_cache["exp"] = now + int(data.get("expires_in", 3600))
    return _token_cache["tok"]


def _meeting_body(topic, start_utc, duration_min) -> dict:
    return {
        "topic": topic,
        "type": 2,  # scheduled meeting
        "start_time": _iso_z(start_utc),
        "duration": int(duration_min),
        "timezone": "UTC",
        "settings": {
            "join_before_host": False,
            "waiting_room": True,
            "approval_type": 2,  # no registration
        },
    }


async def _create_via_s2s(topic, start_utc, duration_min) -> str:
    tok = await _s2s_token()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{_ZOOM_API}/users/me/meetings",
            headers={"Authorization": f"Bearer {tok}"},
            json=_meeting_body(topic, start_utc, duration_min),
        )
        if r.status_code >= 400:
            # Surface Zoom's own error body. A bare raise_for_status once hid a
            # missing-scope 400 behind a generic message and cost a long blind hunt.
            raise RuntimeError(f"zoom s2s create {r.status_code}: {r.text[:300]}")
        url = r.json().get("join_url")
    if not url:
        raise RuntimeError("zoom s2s: no join_url in response")
    return url


async def _create_via_composio(topic, start_utc, duration_min) -> str:
    # Dormant unless a LIVE connection in this project is explicitly wired.
    if not (settings.composio_api_key and settings.composio_zoom_connection
            and settings.composio_zoom_entity):
        raise RuntimeError("composio zoom not configured")
    payload = {
        "connected_account_id": settings.composio_zoom_connection,
        "entity_id": settings.composio_zoom_entity,
        "arguments": {"user_id": "me", **_meeting_body(topic, start_utc, duration_min)},
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            _COMPOSIO_EXEC,
            headers={"x-api-key": settings.composio_api_key, "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
    if data.get("successful") is False:
        raise RuntimeError(f"composio zoom failed: {data.get('error')}")
    url = _find_join_url(data)
    if not url:
        raise RuntimeError("composio zoom: no join_url in response")
    return url


async def meeting_for_booking(event, start_utc, duration_min, participants) -> str:
    """Return a unique Zoom join URL for this booking.

    The meeting topic is the customer name(s) - the booker first, then any
    guests - so the name shown in the Zoom meeting list and in the waiting room
    tells the host exactly who is about to be admitted. Falls back to the
    event-type name only if no participant name is present. Tries S2S, then
    Composio, then the static room. Never raises - a booking always gets a
    usable link.
    """
    names = [p["name"].strip() for p in (participants or [])
             if isinstance(p, dict) and p.get("name") and p["name"].strip()]
    topic = (", ".join(names) if names else event.get("name") or "Meeting")[:190]
    for mint in (_create_via_s2s, _create_via_composio):
        try:
            url = await mint(topic, start_utc, duration_min)
            if url:
                return url
        except Exception as exc:  # noqa: BLE001 - a mint failure must not break the booking
            print(f"[zoom] {mint.__name__} failed: {exc!r}")
    print("[zoom] all dynamic paths failed; falling back to static room")
    return event["location_url"]
