"""/search — job-based car search (review §2).

  POST /search           -> {job_id, status: "pending"}   (returns immediately)
  GET  /search/{job_id}  -> {status, cards, priced_at, ...}  (client polls this)
  GET  /vehicle/{search_key}/{vehicle_id} -> vehicle detail

The heavy upstream call runs in a background task, so we never hold a 4–11s HTTP
connection against Heroku's 30s router timeout.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request

from ..config import Settings, get_settings
from ..models import SearchJob, SearchQuery, VehicleDetail
from ..providers.rapidapi_booking import BookingRapidAPIProvider, ListingExpiredError
from ..guard import is_verified
from ..rate_limit import check_rate_limit
from ..services.search_service import run_search
from ..services.ranking import rank
from ..services import quotes
from ..budget import BudgetExhausted, spend_credit
from .. import jobs
from .errors import forbidden, listing_expired, not_found, rate_limited, upstream_unavailable

logger = logging.getLogger("carfinder.search_router")
router = APIRouter(tags=["search"])

_provider = BookingRapidAPIProvider()


async def _run_job(job_id: str, query: SearchQuery) -> None:
    """Background worker: do the search, then flip the job to done/error."""
    try:
        result = await run_search(query)
        # Rank in-memory: cheapest → closest, best-in-type flagged. Applied here
        # (not in the cache) so every sort mode reuses one cached upstream fetch.
        ranked = rank(
            result.cards,
            origin_lat=query.lat,
            origin_lon=query.lon,
            mode=query.sort,
        )
        # Snapshot server-side prices so checkout is priced by us, not the browser.
        await quotes.store_job_quote(job_id, query, ranked)
        await jobs.mark_done(
            job_id,
            search_key=result.search_key,
            cards=ranked,
            cached=result.cached,
            priced_at=result.priced_at,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("search job %s failed: %s", job_id, exc)
        await jobs.mark_error(job_id, "Search failed — please retry.")


@router.post("/search", response_model=SearchJob, status_code=202)
async def create_search(
    request: Request,
    query: SearchQuery,
    background: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> SearchJob:
    # Credit guard: a search can hit the paid booking API, so only our own
    # pages/apps (Origin/Referer) or an approved API key may start one.
    if not is_verified(request, settings):
        raise forbidden()

    allowed = await check_rate_limit(
        request,
        bucket="search",
        limit=settings.search_rate_limit,
        window=settings.search_rate_window,
    )
    if not allowed:
        raise rate_limited()

    job = await jobs.create_job()
    background.add_task(_run_job, job.job_id, query)
    return job


@router.get("/search/{job_id}", response_model=SearchJob)
async def get_search(job_id: str) -> SearchJob:
    job = await jobs.get_job(job_id)
    if job is None:
        raise not_found("Search job")  # expired or never existed
    return job


async def _guard_detail(request: Request, settings: Settings) -> None:
    """Detail lookups each cost a paid upstream call: same guard + limit as /search,
    plus the daily budget."""
    if not is_verified(request, settings):
        raise forbidden()
    if not await check_rate_limit(request, bucket="detail", limit=settings.detail_rate_limit,
                                  window=settings.detail_rate_window):
        raise rate_limited()
    try:
        await spend_credit()
    except BudgetExhausted as exc:
        raise upstream_unavailable("inventory") from exc


@router.get("/vehicle/{search_key}/{vehicle_id}", response_model=VehicleDetail)
async def vehicle_detail(search_key: str, vehicle_id: str, request: Request,
                         settings: Settings = Depends(get_settings)) -> VehicleDetail:
    await _guard_detail(request, settings)
    try:
        return await _provider.vehicle_detail(search_key, vehicle_id)
    except ListingExpiredError as exc:
        raise listing_expired() from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("vehicle_detail failed (%s/%s): %s", search_key, vehicle_id, exc)
        raise upstream_unavailable("inventory") from exc


@router.get("/supplier/{search_key}/{vehicle_id}")
async def supplier_detail(search_key: str, vehicle_id: str, request: Request,
                          tab: str = "details",
                          settings: Settings = Depends(get_settings)) -> dict:
    await _guard_detail(request, settings)
    try:
        return await _provider.supplier_details(search_key, vehicle_id, tab=tab)
    except ListingExpiredError as exc:
        raise listing_expired() from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("supplier_details failed (%s/%s): %s", search_key, vehicle_id, exc)
        raise upstream_unavailable("inventory") from exc


@router.get("/booking-summary/{search_key}/{vehicle_id}")
async def booking_summary(search_key: str, vehicle_id: str, request: Request,
                          settings: Settings = Depends(get_settings)) -> dict:
    await _guard_detail(request, settings)
    try:
        return await _provider.booking_summary(search_key, vehicle_id)
    except ListingExpiredError as exc:
        raise listing_expired() from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("booking_summary failed (%s/%s): %s", search_key, vehicle_id, exc)
        raise upstream_unavailable("inventory") from exc
