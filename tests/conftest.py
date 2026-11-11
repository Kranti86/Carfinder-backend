"""Test environment: throwaway SQLite DB, in-memory cache, PayPal in record mode.

Must run before any `app.*` import, which is why it lives in conftest.py.
"""

import os
import tempfile

_db = os.path.join(tempfile.mkdtemp(prefix="carfinder-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_db}")
for var in ("REDIS_URL", "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID",
            "PAYPAL_WEBHOOK_ALLOW_UNVERIFIED", "DYNO", "SLACK_WEBHOOK_URL"):
    os.environ.pop(var, None)
# generous limits so tests don't trip each other; the limiter has its own test
for var in ("REQUESTS_RATE_LIMIT", "ORDER_RATE_LIMIT", "LOOKUP_RATE_LIMIT", "DETAIL_RATE_LIMIT"):
    os.environ.setdefault(var, "1000")
