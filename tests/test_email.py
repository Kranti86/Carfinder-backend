"""Brevo API path and SMTP login fallback for outgoing email."""

from __future__ import annotations

import base64

from app.config import get_settings
from app.services import email_service


class _Resp:
    def __init__(self, code=201, text="{}"):
        self.status_code, self.text = code, text


def _cfg(**kw):
    s = get_settings()
    old = {k: getattr(s, k) for k in kw}
    for k, v in kw.items():
        setattr(s, k, v)
    return s, old


def _restore(s, old):
    for k, v in old.items():
        setattr(s, k, v)


def test_brevo_api_used_when_key_set(monkeypatch):
    calls = []
    monkeypatch.setattr(email_service.httpx, "post",
                        lambda url, json, timeout, headers: calls.append((url, json, headers)) or _Resp())
    monkeypatch.setattr(email_service.smtplib, "SMTP",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("SMTP must not be used")))
    s, old = _cfg(email_sender="carrental@reservationdes.online", brevo_api_key="xkeysib-test",
                  email_password="", support_email="help@example.com",
                  email_from_name="Reservation Desk")
    try:
        ok = email_service._send("jane@example.com", "Hi", "<b>x</b>", bcc=["r@trustpilot.com"],
                                 attachments=[(b"%PDF", "c.pdf")])
    finally:
        _restore(s, old)
    assert ok
    url, body, headers = calls[0]
    assert url == email_service.BREVO_URL and headers["api-key"] == "xkeysib-test"
    assert body["sender"] == {"name": "Reservation Desk", "email": "carrental@reservationdes.online"}
    assert body["to"] == [{"email": "jane@example.com"}]
    assert body["bcc"] == [{"email": "r@trustpilot.com"}]
    assert body["replyTo"] == {"email": "help@example.com"}
    assert body["attachment"] == [{"content": base64.b64encode(b"%PDF").decode(), "name": "c.pdf"}]


def test_brevo_error_returns_false(monkeypatch):
    monkeypatch.setattr(email_service.httpx, "post", lambda *a, **k: _Resp(401, "unauthorized"))
    s, old = _cfg(email_sender="a@b.co", brevo_api_key="bad")
    try:
        assert email_service._send("jane@example.com", "Hi", "x") is False
    finally:
        _restore(s, old)


def test_smtp_login_uses_smtp_username(monkeypatch):
    seen = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): pass
        def login(self, u, p): seen["login"] = u
        def sendmail(self, frm, to, msg): seen["from"] = frm
        def quit(self): pass

    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSMTP)
    s, old = _cfg(email_sender="carrental@reservationdes.online", brevo_api_key="",
                  email_password="smtp-key", smtp_username="c28484001@smtp-brevo.com")
    try:
        assert email_service._send("jane@example.com", "Hi", "x")
    finally:
        _restore(s, old)
    assert seen == {"login": "c28484001@smtp-brevo.com", "from": "carrental@reservationdes.online"}
