"""CarFinder API — FastAPI application entrypoint.

Run locally:  uvicorn app.main:app --reload
On Heroku:    see Procfile (uvicorn app.main:app ...)
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .metrics import get_metrics
from .db import init_db
from .providers.http import close_client
from .routers import admin, exports, health, ops, search, suggest, webhooks
from .routers import requests as requests_router

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: ensure Phase 2 tables exist (reservations/agents/audit).
    try:
        await init_db()
    except Exception as exc:  # don't block the API if the DB is briefly unreachable
        logging.getLogger("carfinder").warning("init_db skipped: %s", exc)
    yield
    # shutdown: release the shared HTTP client cleanly.
    await close_client()


app = FastAPI(
    title="CarFinder API",
    version="0.1.0",
    description="Car-rental discovery: location autocomplete + job-based car search.",
    lifespan=lifespan,
    # Interactive docs expose every route + schema; only on when ENABLE_DOCS=true.
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    """Record per-endpoint latency and status class; expose the time to the client.

    Keyed on the matched route *template* (e.g. /search/{job_id}), not the raw
    path, so job-id cardinality doesn't explode the metric set.
    """
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000

    route = request.scope.get("route")
    endpoint = getattr(route, "path", request.url.path)
    metrics = get_metrics()
    metrics.observe_latency(endpoint, duration_ms)
    metrics.incr("requests_total")
    metrics.incr(f"status_{response.status_code // 100}xx")
    response.headers["x-response-time-ms"] = f"{duration_ms:.1f}"
    return response


app.include_router(health.router)
app.include_router(ops.router)
app.include_router(suggest.router)
app.include_router(search.router)
app.include_router(requests_router.router)
app.include_router(admin.router)
app.include_router(exports.router)
app.include_router(webhooks.router)


@app.get("/", tags=["health"])
async def root() -> dict:
    out = {"service": "carfinder", "health": "/health"}
    if settings.enable_docs:
        out["docs"] = "/docs"
    return out
