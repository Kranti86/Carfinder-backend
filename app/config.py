"""Central configuration, loaded from environment variables.

Nothing is hardcoded: locally from `.env`, in production from Heroku Config Vars.
All tunables live here so the rest of the code reads settings, never os.environ.
"""

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Geocoding (Mapbox) ---
    mapbox_token: str = ""

    # --- Inventory (Booking.com via RapidAPI) ---
    rapidapi_key: str = ""
    rapidapi_host: str = "booking-com15.p.rapidapi.com"
    rapidapi_base_url: str = "https://booking-com15.p.rapidapi.com"

    # --- Cache (Redis) ---
    redis_url: str = ""

    # --- TTLs (seconds) ---
    suggest_ttl_seconds: int = 60 * 60 * 24 * 14  # 14 days
    search_ttl_seconds: int = 600                 # 10 min
    job_ttl_seconds: int = 900                    # 15 min

    # --- Upstream resilience ---
    upstream_timeout_seconds: float = 15.0
    upstream_max_retries: int = 2
    breaker_fail_threshold: int = 5
    breaker_reset_seconds: int = 30

    # --- Rate limiting ---
    suggest_rate_limit: int = 30
    suggest_rate_window: int = 60
    search_rate_limit: int = 15
    search_rate_window: int = 60

    # --- Misc ---
    default_currency: str = "USD"
    # Booking V2 derives pricing/currency from the country of residence, not a
    # currency param. Two-letter code; drives searchCarRentals + detail lookups.
    default_country_of_residence: str = "us"
    cors_allow_origins: str = "http://localhost:3000"
    log_level: str = "INFO"
    # If set, /canary requires ?token=<this>. Leave empty to leave it open (dev).
    canary_token: str = ""
    # Coordinates the canary searches (defaults to midtown Manhattan).
    canary_lat: float = 40.7680
    canary_lon: float = -73.9819

    # ---- Credit guard (see app/guard.py) ----
    # /search is allowed only from these domains (or a valid X-API-Key); /stats
    # serves unverified callers from cache only, never a fresh upstream pull.
    guard_enabled: bool = True
    # Accepts either env name: GUARD_ALLOW_DOMAINS or GUARD_ALLOWED_ORIGINS.
    guard_allow_domains: str = Field(
        default="thecarrentals.globetradee.pro,globetradee.pro",
        validation_alias=AliasChoices("guard_allow_domains", "guard_allowed_origins"),
    )
    guard_api_keys: str = ""   # comma-separated keys for approved AI-assistant partners

    # ---- Email (confirmations + review invites) ----
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    email_sender: str = ""          # the address emails are sent FROM (also SMTP login)
    email_password: str = ""        # SMTP/app password
    support_email: str = ""         # shown to customers for help
    brand_name: str = "Therentalcars"
    business_name: str = "Himalayan Haze LLC"
    business_address: str = ""      # shown in email footer / legal
    brand_phone: str = "+1 (888) 527-1977"
    # Trustpilot AFS + TripAdvisor Review Express collector addresses (comma-separated).
    review_bcc: str = ""

    # ---- Google Ads offline conversion export ----
    # Secret token for the auto-pull URLs (/exports/conversions.csv?key=...) that Google Ads
    # scheduled uploads fetch. Leave empty to disable the public URLs (portal download still works).
    conversions_export_token: str = ""
    # Must match the Import conversion action name you create in Google Ads.
    conversions_action_name: str = "Confirmed booking"

    # ---- Server-side quotes / checkout (the client never decides an amount) ----
    # How long a search result stays bookable at its quoted price.
    quote_ttl_seconds: int = 60 * 60 * 2          # 2h
    # How long a created PayPal order stays valid for POST /requests.
    order_ttl_seconds: int = 60 * 60 * 3          # 3h
    insurance_enabled: bool = True
    insurance_label: str = "Full Coverage (CDW + theft)"
    insurance_cdw_net_per_day: float = 8.99       # what the insurer charges us per day
    insurance_markup: float = 0.40                # our markup on the net

    # ---- Abuse limits on the money / PII endpoints (per client) ----
    requests_rate_limit: int = 5                  # POST /requests
    requests_rate_window: int = 600
    order_rate_limit: int = 10                    # POST /paypal/create-order
    order_rate_window: int = 600
    lookup_rate_limit: int = 10                   # POST /lookup
    lookup_rate_window: int = 600
    detail_rate_limit: int = 30                   # /vehicle, /supplier, /booking-summary
    detail_rate_window: int = 60

    # Hard ceiling on paid booking-API calls per UTC day, across all callers.
    # 0 = no ceiling. Caps the damage if someone forges our Origin header.
    upstream_daily_cap: int = 0

    # ---- Ops endpoints ----
    # /metrics requires ?token=<this> (falls back to CANARY_TOKEN). Unset → 404.
    metrics_token: str = ""
    # Interactive API docs (/docs, /redoc, /openapi.json). Off in production.
    enable_docs: bool = False

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def review_bcc_list(self) -> list[str]:
        return [a.strip() for a in self.review_bcc.split(",") if a.strip()]

    @property
    def email_configured(self) -> bool:
        return bool(self.email_sender and self.email_password)

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url)

    @property
    def mapbox_enabled(self) -> bool:
        return bool(self.mapbox_token)

    @property
    def rapidapi_enabled(self) -> bool:
        return bool(self.rapidapi_key)


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so we parse the environment exactly once."""
    return Settings()
