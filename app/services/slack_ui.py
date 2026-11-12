"""Slack Block Kit pieces for bookings: the booking card, its buttons and the modals.

Buttons only appear once Slack interactivity is configured (SLACK_SIGNING_SECRET set),
because a button Slack can't deliver anywhere would just show an error.
"""

from __future__ import annotations

import json
import os

from . import notify


def interactive() -> bool:
    return bool(os.environ.get("SLACK_SIGNING_SECRET"))


def _pt(text: str) -> dict:
    return {"type": "plain_text", "text": text[:75], "emoji": True}


def _md(text: str) -> dict:
    return {"type": "mrkdwn", "text": text[:3000]}


def _confirm(title: str, text: str, ok: str, danger: bool = False) -> dict:
    c = {"title": _pt(title), "text": _md(text), "confirm": _pt(ok), "deny": _pt("Back")}
    if danger:
        c["style"] = "danger"
    return c


def _btn(label: str, action: str, rid: str, style: str = "", confirm: dict | None = None) -> dict:
    b = {"type": "button", "text": _pt(label), "action_id": f"bk:{action}", "value": rid}
    if style:
        b["style"] = style
    if confirm:
        b["confirm"] = confirm
    return b


def _held(r) -> float:
    return r.amount_authorized or r.pay_now_total or 0


def action_buttons(r) -> list[dict]:
    """Buttons that make sense for the booking's current state."""
    rid, ps, st = r.id, r.payment_status, r.status
    closed = st in ("cancelled", "fulfilled")
    out: list[dict] = []
    if not closed:
        out.append(_btn("Claim", "claim", rid))
    if ps == "authorized" and not closed:
        amt = notify.money(_held(r))
        out.append(_btn(f"Capture {amt}", "capture", rid, "primary", _confirm(
            f"Capture {amt}?", f"Charges *{notify._esc(r.customer_name)}*'s card {amt} now.",
            "Capture")))
    if not r.confirmation_sent and not closed:
        out.append(_btn("Send confirmation", "confirm", rid, "primary"))
    if r.confirmation_sent and not closed:
        out.append(_btn("Mark fulfilled", "fulfill", rid, "", _confirm(
            "Mark fulfilled?", "Closes the booking and emails the customer a review invite.",
            "Mark fulfilled")))
    if ps == "captured" and r.capture_ref:
        amt = notify.money(r.amount_captured)
        out.append(_btn(f"Refund {amt}", "refund", rid, "danger", _confirm(
            f"Refund {amt}?", f"Sends {amt} back to *{notify._esc(r.customer_name)}* and "
            "cancels the booking. This can't be undone.", "Refund", danger=True)))
    if not closed:
        out.append(_btn("Cancel booking", "cancel", rid, "danger"))
    out.append(_btn("Add note", "note", rid))
    base = notify.admin_base()
    if base:
        out.append({"type": "button", "text": _pt("Open in admin"), "action_id": "bk:open",
                    "url": f"{base}/admin/reservations/{rid}"})
    return out


def actions_block(r) -> list[dict]:
    if not interactive():
        return []
    return [{"type": "actions", "block_id": f"bk:{r.id}", "elements": action_buttons(r)[:25]}]


def card(r, note: str = "") -> list[dict]:
    """Full booking card."""
    paid = {"authorized": f"held {notify.money(_held(r))}",
            "captured": f"captured {notify.money(r.amount_captured)}",
            "refunded": "refunded", "voided": "hold released", "failed": "card declined",
            "pending": "in PayPal review", "none": "not paid"}.get(r.payment_status,
                                                                  r.payment_status)
    fields = [
        f"*Customer*\n{notify._esc(r.customer_name)}",
        f"*Phone*\n{notify._esc(r.customer_phone) or '—'}",
        f"*Email*\n{notify._esc(r.customer_email) or '—'}",
        f"*Car*\n{notify._esc(r.vehicle_name)} ({notify._esc(r.supplier) or '?'})",
        f"*Where*\n{notify._esc(r.city)}",
        f"*Dates*\n{notify._pickup(r)} → {(r.dropoff_datetime or '').replace('T', ' ')[:16]}",
        f"*Payment*\n{paid}",
        f"*Status*\n{r.status}" + (" · confirmation sent" if r.confirmation_sent else ""),
        f"*Supplier ref*\n{notify._esc(r.supplier_reservation_number) or '—'}",
        f"*Rental price*\n{notify.money(r.base_price)} (pay at counter)",
    ]
    blocks = [
        {"type": "section", "text": _md(f"*Booking {notify.ref_link(r)}*")},
        {"type": "section", "fields": [_md(f) for f in fields]},
    ]
    if note:
        blocks.append({"type": "context", "elements": [_md(note)]})
    return blocks + actions_block(r)


def list_blocks(title: str, rows: list) -> list[dict]:
    blocks = [{"type": "section", "text": _md(f"*{title}* ({len(rows)})")}]
    if not rows:
        blocks.append({"type": "context", "elements": [_md("Nothing here.")]})
    for r in rows[:15]:
        sec = {"type": "section", "text": _md(
            f"{notify.line(r)}\n_{r.payment_status} · {r.status}_")}
        if interactive():
            sec["accessory"] = _btn("Open", "show", r.id)
        blocks.append(sec)
    if len(rows) > 15:
        blocks.append({"type": "context", "elements": [_md(f"…and {len(rows) - 15} more in /admin")]})
    return blocks


# ------------------------------------------------------------------ modals

def _meta(rid: str, response_url: str) -> str:
    return json.dumps({"rid": rid, "response_url": response_url})[:3000]


def cancel_modal(r, response_url: str) -> dict:
    held = r.payment_status in ("authorized", "pending")
    warn = ("The card hold will be *released* and the customer emailed."
            if held else ("Payment was already *captured* — refund separately if money is owed."
                          if r.payment_status == "captured" else "The customer will be emailed."))
    return {
        "type": "modal", "callback_id": "cancel_modal", "private_metadata": _meta(r.id, response_url),
        "title": _pt("Cancel booking"), "submit": _pt("Cancel booking"), "close": _pt("Back"),
        "blocks": [
            {"type": "section", "text": _md(f"{notify.line(r)}\n\n{warn}")},
            {"type": "input", "block_id": "reason", "optional": True, "label": _pt("Reason (sent to customer)"),
             "element": {"type": "plain_text_input", "action_id": "v", "multiline": True}},
        ]}


def note_modal(r, response_url: str) -> dict:
    return {
        "type": "modal", "callback_id": "note_modal", "private_metadata": _meta(r.id, response_url),
        "title": _pt("Add note"), "submit": _pt("Save"), "close": _pt("Back"),
        "blocks": [
            {"type": "section", "text": _md(notify.line(r))},
            {"type": "input", "block_id": "note", "label": _pt("Note"),
             "element": {"type": "plain_text_input", "action_id": "v", "multiline": True}},
        ]}


def confirm_modal(r, response_url: str) -> dict:
    has_doc = bool(r.confirmation_doc)
    supplier = {"type": "plain_text_input", "action_id": "v"}
    if r.supplier_reservation_number:
        supplier["initial_value"] = r.supplier_reservation_number
    policy = {"type": "plain_text_input", "action_id": "v"}
    if r.policy_number:
        policy["initial_value"] = r.policy_number
    doc_label = ("Supplier confirmation PDF (already attached: "
                 f"{r.confirmation_doc_name} — add a new one to replace it)" if has_doc
                 else "Supplier confirmation PDF")
    return {
        "type": "modal", "callback_id": "confirm_modal", "private_metadata": _meta(r.id, response_url),
        "title": _pt("Send final confirmation"), "submit": _pt("Send to customer"),
        "close": _pt("Back"),
        "blocks": [
            {"type": "section", "text": _md(
                f"{notify.line(r)}\n\nThe customer *{notify._esc(r.customer_email)}* gets the "
                "PDF and the supplier reference by email.")},
            {"type": "input", "block_id": "supplier_ref", "label": _pt("Supplier reservation number"),
             "element": supplier},
            {"type": "input", "block_id": "policy", "optional": True,
             "label": _pt("Insurance policy number (if any)"), "element": policy},
            {"type": "input", "block_id": "doc", "optional": has_doc, "label": _pt(doc_label[:150]),
             "element": {"type": "file_input", "action_id": "file", "max_files": 1,
                         "filetypes": ["pdf", "png", "jpg", "jpeg"]}},
        ]}


HELP = """*Therentalcars from Slack* — `/rentals <command>`
• `today` — the daily summary, right now
• `untouched` — bookings nobody has worked yet
• `open` — all open bookings
• `find <ref, name, email or phone>` — search
• `<REF>` (e.g. `8E4341AC`) — one booking with its buttons
• `capture-due` — run the auto-capture now
• `users` — list admin logins
• `add-user <email> <name> [admin]` — create a login (temporary password shown only to you)
• `reset-password <email>` · `disable-user <email>` · `enable-user <email>`
• `whoami` — your Slack member ID
Buttons on each booking: Claim · Capture · Send confirmation · Mark fulfilled · Refund · Cancel · Add note"""
