"""Canary: with no API key configured it must fail gracefully (structured red),
never raise — so a scheduled run always returns a usable status."""

import asyncio

from app.services.canary import run_canary


def test_canary_returns_structured_result_without_key():
    result = asyncio.run(run_canary())
    assert result["ok"] is False
    assert "checked_at" in result
    # either an error (no key) or a reason — but never an exception
    assert "error" in result or "reason" in result
