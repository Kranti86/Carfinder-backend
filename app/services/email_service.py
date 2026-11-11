"""Customer emails: booking confirmation, and the post-rental review invite.

Two emails:
  * send_confirmation(data)  — right after a reservation. Details + reservation number.
  * send_review_invite(data) — when an agent marks the rental fulfilled. BCCs the
    Trustpilot + TripAdvisor collectors so a review request is logged automatically.

Both are sync (blocking SMTP) and meant to run in a background task. If email isn't
configured (no EMAIL_SENDER/EMAIL_PASSWORD) they no-op, so the flow works before setup.
"""

from __future__ import annotations

import base64
import logging
import smtplib

import httpx
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ..config import get_settings

logger = logging.getLogger("carfinder.email")


def _money(v) -> str:
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "$0.00"


BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _send_brevo(s, to: str, subject: str, html: str, bcc: list[str],
                attachments: list[tuple[bytes, str]]) -> bool:
    """Send through Brevo's HTTPS API (works where outbound SMTP is blocked, e.g. Railway
    Free/Trial/Hobby)."""
    body: dict = {
        "sender": {"name": s.email_from_name or s.brand_name, "email": s.email_sender},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html,
    }
    if bcc:
        body["bcc"] = [{"email": a} for a in bcc]
    if s.support_email:
        body["replyTo"] = {"email": s.support_email}
    files = [{"content": base64.b64encode(blob).decode(), "name": name}
             for blob, name in attachments if blob]
    if files:
        body["attachment"] = files
    try:
        r = httpx.post(BREVO_URL, json=body, timeout=20,
                       headers={"api-key": s.brevo_api_key, "accept": "application/json"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("email send failed via Brevo (%s): %s", subject, exc)
        return False
    if r.status_code >= 300:
        logger.warning("email send failed via Brevo (%s): HTTP %s %s",
                       subject, r.status_code, r.text[:300])
        return False
    return True


def _send(to: str, subject: str, html: str, bcc: list[str] | None = None,
          attachments: list[tuple[bytes, str]] | None = None) -> bool:
    s = get_settings()
    if not s.email_configured or not to:
        logger.info("email not sent (unconfigured or no recipient): %s", subject)
        return False
    if s.brevo_api_key:
        return _send_brevo(s, to, subject, html, bcc or [], attachments or [])
    msg = MIMEMultipart("mixed")
    msg["From"] = f"{s.email_from_name or s.brand_name} <{s.email_sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    if s.support_email:
        msg["Reply-To"] = s.support_email
    msg.attach(MIMEText(html, "html"))
    for blob, name in (attachments or []):
        if not blob:
            continue
        part = MIMEApplication(blob, Name=name)
        part["Content-Disposition"] = f'attachment; filename="{name}"'
        msg.attach(part)
    recipients = [to] + (bcc or [])
    try:
        srv = smtplib.SMTP(s.smtp_server, s.smtp_port, timeout=20)
        srv.starttls()
        srv.login(s.smtp_username or s.email_sender, s.email_password)
        srv.sendmail(s.email_sender, recipients, msg.as_string())
        srv.quit()
        return True
    except Exception as exc:
        logger.warning("email send failed (%s): %s", subject, exc)
        return False


def _shell(s, title: str, body: str) -> str:
    support = s.support_email or s.email_sender
    return f"""\
<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;color:#1a2233">
  <div style="background:#0b1d3a;color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
    <div style="font-size:18px;font-weight:800;letter-spacing:1px">{s.brand_name}</div>
    <div style="font-size:13px;opacity:.85">{title}</div>
  </div>
  <div style="border:1px solid #e6ebf3;border-top:0;border-radius:0 0 12px 12px;padding:22px">
    {body}
    <hr style="border:0;border-top:1px solid #eef2f8;margin:22px 0">
    <div style="font-size:12px;color:#7a8298">
      Questions? Contact us at <a href="mailto:{support}">{support}</a> or {s.brand_phone}.<br>
      {s.business_name}{(' · ' + s.business_address) if s.business_address else ''}
    </div>
  </div>
</div>"""


def _rows(pairs) -> str:
    out = ""
    for k, v in pairs:
        out += (f'<tr><td style="padding:7px 0;color:#7a8298;font-size:13px">{k}</td>'
                f'<td style="padding:7px 0;text-align:right;font-weight:600;font-size:13px">{v}</td></tr>')
    return f'<table style="width:100%;border-collapse:collapse">{out}</table>'


def build_confirmation_html(data: dict) -> str:
    s = get_settings()
    ref = (data.get("ref") or (data.get("id") or "")[:8]).upper()
    body = f"""
      <p style="font-size:15px">Hi {data.get('customer_name','') or 'there'},</p>
      <p style="font-size:14px;line-height:1.6">Your reservation is in. Here are the details —
         keep this email; your reservation number is <b>#{ref}</b>.</p>
      <div style="background:#f6f8fc;border-radius:10px;padding:6px 14px;margin:14px 0">
      {_rows([
        ("Vehicle", f"{data.get('vehicle_name','—')}" + (f" · {data.get('vehicle_type')}" if data.get('vehicle_type') else "")),
        ("Supplier", data.get("supplier", "—")),
        ("Where", (data.get("city","") or "—") + ((" · " + data["pickup_area"]) if data.get("pickup_area") else "")),
        ("Pick-up", data.get("pickup_datetime","—")),
        ("Drop-off", data.get("dropoff_datetime","—")),
      ])}
      </div>
      <div style="background:#f6f8fc;border-radius:10px;padding:6px 14px;margin:14px 0">
      {_rows([
        ("Platform fee", _money(data.get("service_fee"))),
        ("Insurance", _money(data.get("insurance_price"))),
        ("Paid now", _money(data.get("pay_now"))),
        ("Rental at counter", _money(data.get("base_price"))),
      ])}
      </div>
      <p style="font-size:13px;color:#42506b;line-height:1.6">The exact pick-up address
        {'and your insurance certificate ' if data.get('insurance_price') else ''}will be
        emailed once your booking is confirmed with the supplier. The rental balance is paid
        at the counter.</p>
    """
    return _shell(s, "Reservation confirmed", body)


def build_final_html(data: dict) -> str:
    s = get_settings()
    ref = (data.get("ref") or (data.get("id") or "")[:8]).upper()
    body = f"""
      <p style="font-size:15px">Hi {data.get('customer_name','') or 'there'},</p>
      <p style="font-size:14px;line-height:1.6">Good news — your rental is <b>confirmed with the
         supplier</b>. Your booking documents are attached to this email. Please bring your
         driver's license and the credit card in the main driver's name to the counter.</p>
      <div style="background:#f6f8fc;border-radius:10px;padding:6px 14px;margin:14px 0">
      {_rows([
        ("Our reservation #", ref),
        ("Supplier confirmation #", data.get("supplier_reservation_number","—") or "—"),
        ("Vehicle", f"{data.get('vehicle_name','—')}" + (f" · {data.get('vehicle_type')}" if data.get('vehicle_type') else "")),
        ("Supplier", data.get("supplier","—")),
        ("Pick-up", (data.get("pickup_area") or data.get("city","—"))),
        ("Pick-up time", data.get("pickup_datetime","—")),
        ("Drop-off time", data.get("dropoff_datetime","—")),
      ] + ([("Insurance policy #", data["policy_number"])] if data.get("policy_number") else []))}
      </div>
      <p style="font-size:13px;color:#42506b;line-height:1.6">The rental balance of
         {_money(data.get('base_price'))} is paid to the supplier at the counter. Keep this email
         and the attached confirmation for pick-up.</p>
    """
    return _shell(s, "Your rental is confirmed", body)


def send_final_confirmation(data: dict, doc: bytes | None = None,
                            doc_name: str = "confirmation.pdf") -> bool:
    s = get_settings()
    ref = (data.get("ref") or "").upper()
    atts = [(doc, doc_name)] if doc else None
    return _send(data.get("customer_email", ""),
                 f"Your {s.brand_name} rental is confirmed — #{ref}",
                 build_final_html(data), attachments=atts)


def build_cancellation_html(data: dict) -> str:
    s = get_settings()
    ref = (data.get("ref") or (data.get("id") or "")[:8]).upper()
    reason = data.get("reason", "")
    body = f"""
      <p style="font-size:15px">Hi {data.get('customer_name','') or 'there'},</p>
      <p style="font-size:14px;line-height:1.6">Your reservation <b>#{ref}</b>
         {('for the ' + data['vehicle_name']) if data.get('vehicle_name') else ''}
         {('in ' + data['city']) if data.get('city') else ''} has been <b>cancelled</b>.</p>
      {f'<p style="font-size:13px;color:#42506b">Reason: {reason}</p>' if reason else ''}
      <p style="font-size:14px;line-height:1.6">If a payment hold or charge was placed, it is
         being released or refunded to your original payment method — this typically takes
         5–10 business days depending on your bank. You have not been charged for the rental
         itself.</p>
      <p style="font-size:13px;color:#42506b">If this was a mistake or you'd like to rebook,
         just reply to this email and we'll help.</p>
    """
    return _shell(s, "Reservation cancelled", body)


def send_cancellation(data: dict) -> bool:
    s = get_settings()
    ref = (data.get("ref") or "").upper()
    return _send(data.get("customer_email", ""),
                 f"Your {s.brand_name} reservation #{ref} has been cancelled",
                 build_cancellation_html(data))


def build_review_html(data: dict) -> str:
    s = get_settings()
    body = f"""
      <p style="font-size:15px">Hi {data.get('customer_name','') or 'there'},</p>
      <p style="font-size:14px;line-height:1.6">Thanks for renting with {s.brand_name}!
         We hope the {data.get('vehicle_name','car')} in {data.get('city','')} treated you well.</p>
      <p style="font-size:14px;line-height:1.6">A quick review helps other travelers and takes
         under a minute — just reply to this email with a few words about your experience,
         or rate us on Trustpilot / TripAdvisor.</p>
      <p style="font-size:13px;color:#7a8298">Reservation #{(data.get('ref') or '').upper()}</p>
    """
    return _shell(s, "How was your rental?", body)


# ----------------------------------------------------------------- public API
def send_confirmation(data: dict) -> bool:
    return _send(data.get("customer_email", ""),
                 f"Your {get_settings().brand_name} reservation "
                 f"#{(data.get('ref') or '').upper()}",
                 build_confirmation_html(data))


def send_review_invite(data: dict) -> bool:
    s = get_settings()
    return _send(data.get("customer_email", ""),
                 f"How was your rental with {s.brand_name}?",
                 build_review_html(data), bcc=s.review_bcc_list)
