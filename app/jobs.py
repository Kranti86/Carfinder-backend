"""Job store for the submit-then-poll search flow (review §2).

POST /search creates a job (status=pending) and kicks off the upstream call in
the background; the client polls GET /search/{job_id}. Job state lives in Redis
so any dyno can serve the poll for a job any other dyno started.

Note: the upstream aggregates and returns one blob, so a job flips pending→done
in a single step — there is no true per-supplier streaming to expose.
"""

from __future__ import annotations

import uuid

from .cache import get_cache
from .config import get_settings
from .models import CarCard, JobStatus, SearchJob


def new_job_id() -> str:
    return uuid.uuid4().hex


def _job_key(job_id: str) -> str:
    return f"job:v1:{job_id}"


async def create_job() -> SearchJob:
    job = SearchJob(job_id=new_job_id(), status=JobStatus.pending)
    await _save(job)
    return job


async def _save(job: SearchJob) -> None:
    cache = get_cache()
    ttl = get_settings().job_ttl_seconds
    await cache.set_json(_job_key(job.job_id), job.model_dump(), ttl)


async def get_job(job_id: str) -> SearchJob | None:
    data = await get_cache().get_json(_job_key(job_id))
    return SearchJob(**data) if data else None


async def mark_done(
    job_id: str,
    *,
    search_key: str | None,
    cards: list[CarCard],
    cached: bool,
    priced_at: str | None,
) -> None:
    job = await get_job(job_id)
    if job is None:
        return
    job.status = JobStatus.done
    job.search_key = search_key
    job.cards = cards
    job.cached = cached
    job.priced_at = priced_at
    await _save(job)


async def mark_error(job_id: str, error: str) -> None:
    job = await get_job(job_id)
    if job is None:
        return
    job.status = JobStatus.error
    job.error = error
    await _save(job)
