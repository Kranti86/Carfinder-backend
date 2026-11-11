"""Shared async HTTP helper: one client, with timeout, bounded retries, and a
circuit breaker in front of it.

Retries only idempotent failures (timeouts, connect errors, 5xx, 429) with a
short backoff. The breaker is consulted before dialling so a known-down upstream
fails fast instead of piling up doomed requests.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..circuit_breaker import CircuitBreaker, CircuitOpenError
from ..config import get_settings
from ..metrics import get_metrics

logger = logging.getLogger("carfinder.http")

# RapidAPI surfaces remaining quota in response headers; capture whichever exist.
_QUOTA_HEADERS = (
    "x-ratelimit-requests-remaining",
    "x-ratelimit-rapid-free-plans-hard-limit-remaining",
)


def _record_quota(headers) -> None:
    metrics = get_metrics()
    for name in _QUOTA_HEADERS:
        value = headers.get(name)
        if value is not None:
            try:
                metrics.set_gauge(f"quota_remaining::{name}", float(value))
            except (TypeError, ValueError):
                pass

_client: httpx.AsyncClient | None = None

# Backoff is deterministic (no jitter) so behaviour is test-reproducible.
_BACKOFF_SECONDS = (0.25, 0.75, 1.5)


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        timeout = get_settings().upstream_timeout_seconds
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class UpstreamError(RuntimeError):
    """Any non-recoverable failure talking to an upstream provider."""


async def request_json(
    method: str,
    url: str,
    *,
    breaker: CircuitBreaker | None = None,
    params: dict | None = None,
    headers: dict | None = None,
    json_body: dict | None = None,
) -> dict:
    """Make a request and return parsed JSON, with retry + breaker.

    Raises CircuitOpenError if the breaker is open, UpstreamError otherwise.
    """
    settings = get_settings()
    client = get_client()

    if breaker is not None:
        await breaker.guard()  # raises CircuitOpenError if open

    attempts = settings.upstream_max_retries + 1
    last_exc: Exception | None = None
    metrics = get_metrics()

    for attempt in range(attempts):
        try:
            metrics.incr("upstream_calls")
            resp = await client.request(
                method, url, params=params, headers=headers, json=json_body
            )
            _record_quota(resp.headers)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise UpstreamError(f"{resp.status_code} from {url}")
            resp.raise_for_status()
            if breaker is not None:
                await breaker.record_success()
            return resp.json()
        except (httpx.TimeoutException, httpx.TransportError, UpstreamError) as exc:
            last_exc = exc
            metrics.incr("upstream_errors")
            if breaker is not None:
                await breaker.record_failure()
            if attempt < attempts - 1:
                await asyncio.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
                logger.info("retrying %s %s (attempt %d): %s", method, url, attempt + 2, exc)
                continue
        except httpx.HTTPStatusError as exc:
            # 4xx (other than 429) — not retryable, don't trip the breaker.
            metrics.incr("upstream_errors")
            raise UpstreamError(f"{exc.response.status_code} from {url}") from exc

    raise UpstreamError(f"upstream failed after {attempts} attempts: {last_exc}")
