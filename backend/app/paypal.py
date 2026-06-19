"""PayPal Orders v2: create an order, capture it, verify it actually paid.

No secret in the repo. Both the API base and the credentials come from the
environment. A paid booking is finalized only when a capture comes back
COMPLETED with the exact amount and currency we asked for. A refund path is
here for the rare case where money is taken but the slot has gone.
"""
import base64
from decimal import Decimal

import httpx

from .config import settings

_TIMEOUT = httpx.Timeout(20.0)


def _base() -> str:
    return ("https://api-m.paypal.com" if settings.paypal_env == "live"
            else "https://api-m.sandbox.paypal.com")


def configured() -> bool:
    return bool(settings.paypal_client_id and settings.paypal_client_secret)


def _amount(cents: int) -> str:
    return f"{Decimal(int(cents)) / 100:.2f}"


async def _token() -> str:
    basic = base64.b64encode(
        f"{settings.paypal_client_id}:{settings.paypal_client_secret}".encode()
    ).decode()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{_base()}/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"paypal token {r.status_code}: {r.text[:300]}")
        return r.json()["access_token"]


async def create_order(amount_cents: int, currency: str, reference: str,
                       description: str) -> dict:
    tok = await _token()
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": reference,
            "custom_id": reference,
            "description": (description or "Meeting")[:127],
            "amount": {"currency_code": currency, "value": _amount(amount_cents)},
        }],
        "application_context": {
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
            "brand_name": "IFB Bank",
        },
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{_base()}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {tok}",
                     "Content-Type": "application/json"},
            json=body,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"paypal create {r.status_code}: {r.text[:300]}")
        return r.json()


async def capture_order(order_id: str) -> dict:
    tok = await _token()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{_base()}/v2/checkout/orders/{order_id}/capture",
            headers={"Authorization": f"Bearer {tok}",
                     "Content-Type": "application/json"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"paypal capture {r.status_code}: {r.text[:400]}")
        return r.json()


def extract_capture(order_json: dict):
    """Return (status, capture_id, currency, value_str) from a capture response.

    Prefers the capture object's own status and amount; falls back to the
    order-level status when no capture object is present.
    """
    status = order_json.get("status")
    capture_id = currency = value = None
    for pu in order_json.get("purchase_units", []):
        caps = (pu.get("payments") or {}).get("captures") or []
        for cap in caps:
            capture_id = cap.get("id")
            amt = cap.get("amount") or {}
            currency = amt.get("currency_code")
            value = amt.get("value")
            if cap.get("status"):
                status = cap["status"]
            break
    return status, capture_id, currency, value


async def refund(capture_id: str) -> dict:
    tok = await _token()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{_base()}/v2/payments/captures/{capture_id}/refund",
            headers={"Authorization": f"Bearer {tok}",
                     "Content-Type": "application/json"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"paypal refund {r.status_code}: {r.text[:300]}")
        return r.json()
