"""Configuration for WeatherGPT.

Every external dependency is optional and degrades gracefully so the demo
always runs. Set what you have in .env; the rest falls back to open sources.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Identity -------------------------------------------------------
    app_name: str = "WeatherGPT"
    ps_id: str = "SIH26068"

    # --- IMD (primary authoritative source) -----------------------------
    # Register at https://api.imd.gov.in/public/register.php for a key.
    # Without a key the IMD provider is skipped and NWP fallback is used.
    imd_api_base: str = "https://api.imd.gov.in/api/v1"
    imd_api_key: str | None = None

    # --- NWP / open meteorological sources ------------------------------
    # Open-Meteo serves GFS/ECMWF/ICON NWP output + ERA5 reanalysis archive.
    openmeteo_forecast_base: str = "https://api.open-meteo.com/v1/forecast"
    openmeteo_archive_base: str = "https://archive-api.open-meteo.com/v1/archive"
    openmeteo_geocode_base: str = "https://geocoding-api.open-meteo.com/v1/search"
    openmeteo_aq_base: str = "https://air-quality-api.open-meteo.com/v1/air-quality"

    # --- Language layer -------------------------------------------------
    # Bhashini (MeitY) ASR + NMT + TTS. Falls back to on-device Web Speech
    # API in the browser and to bundled phrase templates for text.
    bhashini_user_id: str | None = None
    bhashini_api_key: str | None = None
    bhashini_config_url: str = (
        "https://meity-auth.ulcacontrib.org/ulca/apis/v0/model/getModelsPipeline"
    )

    # --- LLM (intent parsing + open-ended phrasing only) ----------------
    # NEVER used to produce numbers. See nlu.py for the grounding contract.
    llm_provider: str = "none"           # none | groq | openai | gemini | ollama
    llm_api_key: str | None = None
    llm_model: str = "llama-3.3-70b-versatile"
    llm_base_url: str | None = None

    # --- Safety ---------------------------------------------------------
    # /api/alerts/simulate injects a synthetic RED cyclone alert and fans it
    # out to every matching subscriber. That is exactly what you want on a
    # demo stage and exactly what you must never expose on a public
    # deployment: an open endpoint that broadcasts a fake national warning.
    # Set ENABLE_DEMO_ENDPOINTS=false for anything reachable from outside.
    enable_demo_endpoints: bool = True

    # --- Deployment guards ----------------------------------------------
    # Comma-separated list of origins allowed to call the API from a browser.
    # Resolution order (see allowed_origins() below):
    #   1. CORS_ALLOW_ORIGINS, if set  -- always wins
    #   2. RENDER_EXTERNAL_URL, which Render injects as the service's full
    #      https:// URL -- so the deployment is pinned to itself with no
    #      manual step and no chicken-and-egg on first deploy
    #   3. "*", so a laptop clone still runs unconfigured
    cors_allow_origins: str = ""
    render_external_url: str = ""

    # Shared secret for the dissemination endpoints (simulate / subscribe /
    # scan). Unset = gate open, which is what keeps local dev frictionless.
    # NEVER hardcode a value here or commit one; set it in the platform's
    # secret store. See backend/app/security.py for the rationale.
    demo_token: str | None = None

    # Per-IP ceilings; 0 disables a limiter. The binding constraint is the
    # upstream NWP provider's own per-IP limit, which the whole deployment
    # shares -- not CPU. 60/min bounds a single scraper to roughly 60
    # cache-missing upstream calls a minute, comfortably inside Open-Meteo's
    # allowance, while staying clear of a judge clicking through demo prompts.
    # Set RATE_LIMIT_CHAT_PER_MIN=0 when running tests/test_smoke.py, which
    # drives the endpoint far faster than a human ever would.
    rate_limit_chat_per_min: int = 60
    rate_limit_subscribe_per_min: int = 10

    # --- Runtime --------------------------------------------------------
    cache_ttl_seconds: int = 600
    # Short per-attempt timeout: a hung upstream connection should fail
    # fast and be retried, not hold a user waiting. Total budget is
    # governed by the retry count in providers/openmeteo.py.
    http_timeout: float = 8.0
    default_lang: str = "en"

    class Config:
        env_file = os.getenv("WEATHERGPT_ENV_FILE", ".env")
        env_prefix = ""
        extra = "ignore"


    def allowed_origins(self) -> list[str]:
        """Browser origins permitted to call this API.

        Explicit configuration beats inference: if CORS_ALLOW_ORIGINS is set it
        is used verbatim, including a deliberate "*". Otherwise, on Render,
        RENDER_EXTERNAL_URL pins the deployment to its own origin. Off-platform
        with nothing configured, fall back to "*" -- the local-dev default the
        README promises.
        """
        if self.cors_allow_origins.strip():
            return [o.strip() for o in self.cors_allow_origins.split(",")
                    if o.strip()]
        if self.render_external_url.strip():
            return [self.render_external_url.strip().rstrip("/")]
        return ["*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
