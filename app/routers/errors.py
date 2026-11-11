"""Shared HTTP error helpers, so routers raise consistent responses."""

from __future__ import annotations

from fastapi import HTTPException, status


def rate_limited() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests — slow down and try again shortly.",
    )


def upstream_unavailable(provider: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"The {provider} service is temporarily unavailable. Please retry.",
    )


def not_found(what: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found.")


def forbidden() -> HTTPException:
    # 403: caller isn't one of our pages/apps and has no API key — likely a scraper.
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="This endpoint is available to Therentalcars pages and approved partners only.",
    )


def listing_expired() -> HTTPException:
    # 410 Gone: the search results this vehicle belonged to have expired.
    return HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="This listing has expired. Please run the search again to see current prices.",
    )
