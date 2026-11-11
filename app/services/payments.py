"""Payment layer — PayPal (Orders v2: authorize hold → capture / void / refund).

Flow the roadmap needs, mapped to PayPal:
  * Reservation: create an order with intent=AUTHORIZE, buyer approves, then we
    `authorize` it → an AUTHORIZATION id (the hold). We DON'T capture yet.
  * After ~3 days (or manually): `capture` the authorization → a CAPTURE id.
  * Refund operates on the CAPTURE id; Void operates on the AUTHORIZATION id.

Ids we track on the reservation:
  * payment_ref  = authorization id (hold)  — used by capture() and void()
  * capture_ref  = capture id               — used by refund()

Safe-before-credentials: with no PAYPAL_CLIENT_ID/SECRET set, every call runs in
"recorded, not charged" mode (returns a deterministic pseudo id), so the whole
portal + /requests flow works today and goes live the moment you add the keys.

PayPal note: an authorization's honor period is ~3 days (funds guaranteed). Capturing
right at day 3 is the edge of that window — run the auto-capture job at/under 3 days,
or reauthorize for longer holds.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass

import httpx


@dataclass
class PayResult:
    ok: bool
    ref: str = ""          # authorization id, capture id, or refund id depending on op
    status: str = ""
    message: str = ""
    live: bool = False     # True if it actually hit PayPal
    amount: float | None = None  # amount PayPal actually authorized/captured, if reported


def _creds():
    return (os.environ.get("PAYPAL_CLIENT_ID"), os.environ.get("PAYPAL_CLIENT_SECRET"))


def is_configured() -> bool:
    cid, secret = _creds()
    return bool(cid and secret)


def _base() -> str:
    env = (os.environ.get("PAYPAL_ENV", "sandbox") or "sandbox").lower()
    return "https://api-m.paypal.com" if env == "production" else "https://api-m.sandbox.paypal.com"


_token_cache = {"token": "", "exp": 0.0}


async def _access_token(client: httpx.AsyncClient) -> str | None:
    if _token_cache["token"] and _token_cache["exp"] > time.time() + 30:
        return _token_cache["token"]
    cid, secret = _creds()
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = await client.post(f"{_base()}/v1/oauth2/token",
                          headers={"Authorization": f"Basic {basic}",
                                   "Content-Type": "application/x-www-form-urlencoded"},
                          data={"grant_type": "client_credentials"})
    if r.status_code != 200:
        return None
    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["exp"] = time.time() + float(data.get("expires_in", 3000))
    return _token_cache["token"]


async def _post(path: str, body: dict | None = None) -> tuple[bool, dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        tok = await _access_token(client)
        if not tok:
            return False, {"message": "paypal auth failed"}
        r = await client.post(f"{_base()}{path}",
                              headers={"Authorization": f"Bearer {tok}",
                                       "Content-Type": "application/json"},
                              json=body or {})
        ok = r.status_code in (200, 201, 202, 204)
        try:
            data = r.json() if r.content else {}
        except Exception:
            data = {}
        if not ok:
            data.setdefault("message", f"paypal http {r.status_code}")
        return ok, data


async def _get(path: str) -> tuple[bool, dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        tok = await _access_token(client)
        if not tok:
            return False, {"message": "paypal auth failed"}
        r = await client.get(f"{_base()}{path}", headers={"Authorization": f"Bearer {tok}"})
        try:
            data = r.json() if r.content else {}
        except Exception:
            data = {}
        return r.status_code == 200, data


# Authorization statuses that mean money is really held for us.
HELD_STATUSES = ("CREATED",)          # funds held, capturable
PENDING_STATUSES = ("PENDING",)       # under review by PayPal — not guaranteed yet
RELEASED_STATUSES = ("DENIED", "VOIDED", "EXPIRED")  # nothing held any more


def _decline_reason(auth: dict) -> str:
    pr = auth.get("processor_response") or {}
    bits = [f"status {auth.get('status', '?')}"]
    if pr.get("response_code"):
        bits.append(f"processor {pr['response_code']}")
    if pr.get("avs_code"):
        bits.append(f"AVS {pr['avs_code']}")
    if pr.get("cvv_code"):
        bits.append(f"CVV {pr['cvv_code']}")
    return "card hold not approved: " + ", ".join(bits)


def _amt(value: float) -> dict:
    return {"currency_code": "USD", "value": f"{value:.2f}"}


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #
def _payer(customer: dict | None) -> dict:
    """Carry the customer's details forward so PayPal doesn't re-ask for them."""
    if not customer:
        return {}
    p: dict = {}
    name = (customer.get("name") or "").strip()
    if name:
        parts = name.split()
        p["name"] = {"given_name": parts[0],
                     "surname": " ".join(parts[1:]) if len(parts) > 1 else parts[0]}
    if customer.get("email"):
        p["email_address"] = customer["email"]
    phone = (customer.get("phone") or "").strip()
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())[-10:]
        if digits:
            p["phone"] = {"phone_type": "MOBILE", "phone_number": {"national_number": digits}}
    return p


async def create_order(amount: float, reservation_id: str, customer: dict | None = None) -> PayResult:
    """Create an AUTHORIZE-intent order for the buyer to approve. Returns order id."""
    if not is_configured():
        return PayResult(ok=True, ref=f"ORDER-{reservation_id[:12]}", status="CREATED",
                         message="recorded (PayPal not configured)", live=False)
    body = {
        "intent": "AUTHORIZE",
        "purchase_units": [{"amount": _amt(amount), "custom_id": reservation_id}],
    }
    payer = _payer(customer)
    if payer:
        body["payer"] = payer
    ok, data = await _post("/v2/checkout/orders", body)
    if ok and data.get("id"):
        return PayResult(ok=True, ref=data["id"], status=data.get("status", ""), live=True)
    return PayResult(ok=False, message=data.get("message", "create order failed"), live=True)


async def authorize(amount: float, order_id: str, *, reservation_id: str) -> PayResult:
    """Authorize an APPROVED order → an authorization id (the hold)."""
    if not is_configured():
        return PayResult(ok=True, ref=f"AUTH-{reservation_id[:12]}", status="authorized",
                         message="recorded (PayPal not configured)", live=False, amount=amount)
    ok, data = await _post(f"/v2/checkout/orders/{order_id}/authorize")
    if ok:
        try:
            auth = data["purchase_units"][0]["payments"]["authorizations"][0]
        except (KeyError, IndexError):
            return PayResult(ok=False, message="no authorization in response", live=True)
        try:
            held = float((auth.get("amount") or {}).get("value"))
        except (TypeError, ValueError):
            held = None
        status = str(auth.get("status", "")).upper()
        # PayPal answers 201 even when the bank DECLINES the hold — the real result is
        # the authorization's own status. Only CREATED (or PENDING review) holds money.
        if status not in HELD_STATUSES + PENDING_STATUSES:
            return PayResult(ok=False, ref=auth.get("id", ""), status=status,
                             message=_decline_reason(auth), live=True, amount=held)
        return PayResult(ok=True, ref=auth["id"], status=status, live=True, amount=held)
    return PayResult(ok=False, message=data.get("message", "authorize failed"), live=True)


async def capture(authorization_id: str, amount: float | None = None) -> PayResult:
    """Capture an authorization → a capture id (used later for refunds)."""
    if not is_configured():
        return PayResult(ok=True, ref=f"CAP-{authorization_id[-8:]}", status="COMPLETED",
                         message="recorded (PayPal not configured)", live=False)
    body = {"final_capture": True}
    if amount is not None:
        body["amount"] = _amt(amount)
    ok, data = await _post(f"/v2/payments/authorizations/{authorization_id}/capture", body)
    if ok and data.get("id"):
        return PayResult(ok=True, ref=data["id"], status=data.get("status", ""), live=True)
    return PayResult(ok=False, message=data.get("message", "capture failed"), live=True)


async def authorization_status(authorization_id: str) -> str:
    """Current status of an authorization (CREATED, DENIED, VOIDED, EXPIRED, CAPTURED…).
    Empty string if it can't be read."""
    if not is_configured() or not authorization_id:
        return ""
    ok, data = await _get(f"/v2/payments/authorizations/{authorization_id}")
    return str(data.get("status", "")).upper() if ok else ""


async def void(authorization_id: str) -> PayResult:
    """Void an authorization (cancel a hold before capture)."""
    if not is_configured():
        return PayResult(ok=True, ref=authorization_id, status="VOIDED",
                         message="recorded (PayPal not configured)", live=False)
    ok, data = await _post(f"/v2/payments/authorizations/{authorization_id}/void")
    if ok:
        return PayResult(ok=True, ref=authorization_id, status="VOIDED", live=True)
    return PayResult(ok=False, message=data.get("message", "void failed"), live=True)


def _allow_unverified() -> bool:
    return (os.environ.get("PAYPAL_WEBHOOK_ALLOW_UNVERIFIED", "") or "").lower() in ("1", "true", "yes")


async def verify_webhook(headers, event: dict) -> bool:
    """Verify a webhook came from PayPal. Without PayPal credentials + PAYPAL_WEBHOOK_ID
    we cannot verify, so the event is REJECTED — otherwise anyone could post a fake
    "captured"/"refunded" event. For local testing only, PAYPAL_WEBHOOK_ALLOW_UNVERIFIED=true
    lets unverifiable events through."""
    wid = os.environ.get("PAYPAL_WEBHOOK_ID")
    if not is_configured() or not wid:
        return _allow_unverified()
    def h(k):
        return headers.get(k) or headers.get(k.lower()) or headers.get(k.title())
    ok, data = await _post("/v1/notifications/verify-webhook-signature", {
        "auth_algo": h("paypal-auth-algo"),
        "cert_url": h("paypal-cert-url"),
        "transmission_id": h("paypal-transmission-id"),
        "transmission_sig": h("paypal-transmission-sig"),
        "transmission_time": h("paypal-transmission-time"),
        "webhook_id": wid,
        "webhook_event": event,
    })
    return ok and data.get("verification_status") == "SUCCESS"


def webhook_verification_enabled() -> bool:
    return bool(is_configured() and os.environ.get("PAYPAL_WEBHOOK_ID"))


async def refund(capture_id: str, amount: float | None = None) -> PayResult:
    """Refund a captured payment (operates on the capture id)."""
    if not is_configured():
        return PayResult(ok=True, ref=f"REF-{capture_id[-8:]}", status="COMPLETED",
                         message="recorded (PayPal not configured)", live=False)
    body = {"amount": _amt(amount)} if amount is not None else {}
    ok, data = await _post(f"/v2/payments/captures/{capture_id}/refund", body)
    if ok and data.get("id"):
        return PayResult(ok=True, ref=data["id"], status=data.get("status", ""), live=True)
    return PayResult(ok=False, message=data.get("message", "refund failed"), live=True)
