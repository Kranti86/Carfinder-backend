"""Token-protected CSV endpoints Google Ads scheduled uploads fetch on their own.

Google Ads → Goals → Conversions → Uploads → Schedules → add an HTTPS source pointing at:
    https://<backend>/exports/conversions.csv?key=<CONVERSIONS_EXPORT_TOKEN>
    https://<backend>/exports/retractions.csv?key=<CONVERSIONS_EXPORT_TOKEN>
set it daily/weekly, and Google pulls the file — no manual import, no Google Ads API.

Access is gated by a shared secret (CONVERSIONS_EXPORT_TOKEN). If the token is unset the
endpoints return 404 so they can't be probed. The same CSVs are also downloadable one-click
from the admin portal (see routers/admin.py) behind agent login.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..services import conversions

router = APIRouter(tags=["exports"], prefix="/exports")


def _check(key: str) -> None:
    token = get_settings().conversions_export_token
    # No token configured → feature disabled → look like it doesn't exist.
    if not token:
        raise HTTPException(status_code=404, detail="Not found")
    # Constant-time compare so the token can't be guessed by timing.
    if not (key and secrets.compare_digest(key, token)):
        raise HTTPException(status_code=403, detail="Bad key")


def _name() -> str:
    return get_settings().conversions_action_name or conversions.DEFAULT_CONVERSION_NAME


@router.get("/conversions.csv", response_class=PlainTextResponse)
async def conversions_feed(key: str = Query(""), session: AsyncSession = Depends(get_session)):
    _check(key)
    text, _ = await conversions.conversions_csv(session, _name())
    return PlainTextResponse(text, media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="ads_conversions.csv"'})


@router.get("/retractions.csv", response_class=PlainTextResponse)
async def retractions_feed(key: str = Query(""), session: AsyncSession = Depends(get_session)):
    _check(key)
    text, _ = await conversions.retractions_csv(session, _name())
    return PlainTextResponse(text, media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="ads_conversion_retractions.csv"'})
