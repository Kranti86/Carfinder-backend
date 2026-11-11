"""Per-agent authentication for the ops portal.

- Passwords are bcrypt-hashed.
- A login sets a signed, timestamped session cookie (itsdangerous) holding the
  agent id — no server-side session store needed.
- `current_agent` is a dependency the portal routes use; unauthenticated callers
  are redirected to the login page.

Every money action in the portal records which agent did it (see AuditLog), which
is the whole point of per-agent accounts.
"""

from __future__ import annotations

import logging
import os
import secrets

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models_db import Agent

COOKIE_NAME = "carfinder_session"
SESSION_MAX_AGE = 60 * 60 * 8  # 8 hours

_SECRET = os.environ.get("SESSION_SECRET")
if not _SECRET:
    if os.environ.get("DYNO"):
        # On Heroku: refuse to boot rather than sign admin cookies with a guessable key.
        raise RuntimeError("SESSION_SECRET must be set (Heroku → Settings → Config Vars).")
    # Local dev: a random per-process secret (you just log in again after a restart).
    _SECRET = secrets.token_urlsafe(48)
    logging.getLogger("carfinder.auth").warning(
        "SESSION_SECRET not set — using a random dev secret; sessions reset on restart.")
_serializer = URLSafeTimedSerializer(_SECRET, salt="carfinder-session")


# --------------------------------------------------------------------------- #
# password helpers
# --------------------------------------------------------------------------- #
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# session cookie
# --------------------------------------------------------------------------- #
def make_session(agent_id: int) -> str:
    return _serializer.dumps({"aid": agent_id})


def read_session(token: str) -> int | None:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return int(data["aid"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# lookups
# --------------------------------------------------------------------------- #
async def authenticate(session: AsyncSession, email: str, password: str) -> Agent | None:
    email = (email or "").strip().lower()
    agent = (await session.execute(
        select(Agent).where(Agent.email == email))).scalar_one_or_none()
    if agent and agent.active and verify_password(password, agent.password_hash):
        return agent
    return None


async def agent_from_request(request, session: AsyncSession) -> Agent | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    aid = read_session(token)
    if aid is None:
        return None
    agent = await session.get(Agent, aid)
    return agent if (agent and agent.active) else None
