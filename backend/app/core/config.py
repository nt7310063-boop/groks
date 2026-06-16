from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "GrokFlow"
    APP_ENV: str = "development"
    APP_DEBUG: bool = True

    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379/0"

    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRES_MINUTES: int = 1440

    API_KEY_PREFIX: str = "uxpm_live"
    ENCRYPTION_KEY: str

    STORAGE_DRIVER: str = "local"
    LOCAL_STORAGE_PATH: str = "./storage"

    CHROME_BINARY_PATH: str = ""
    PROFILE_BASE_PATH: str = "./browser_profiles"

    MAX_CONCURRENT_JOBS_PER_USER: int = 2
    MAX_CONCURRENT_JOBS_PER_PROFILE: int = 1
    DEFAULT_DAILY_LIMIT: int = 100
    JOB_TIMEOUT_SECONDS: int = 600

    # Worker retry policy. Comma-separated list of delay-seconds; the Nth
    # retry uses the Nth value (or the last one when retries exceed length).
    #   JOB_BACKOFF_SECONDS         — generic transient failures
    #   JOB_RATE_LIMIT_BACKOFF      — when the vendor returns 429
    # Defaults match the historical hard-coded tables in workers/run.py.
    JOB_BACKOFF_SECONDS: str = "10,30,60"
    JOB_RATE_LIMIT_BACKOFF: str = "30,60,120"
    # Error codes the worker should NEVER retry (terminal — fail fast).
    JOB_TERMINAL_ERROR_CODES: str = (
        "cookie_expired,captcha_required,provider_blocked,"
        "unsupported_job_type,content_moderated"
    )

    CORS_ORIGINS: str = "http://localhost:5173"

    # Canonical public origin (scheme+host, no trailing slash) for absolute
    # URLs the API hands back to partners — file URLs, callback hints, etc.
    # When set, overrides Host-header sniffing so internal hostnames like
    # `backend:8000` (from intra-cluster calls or misconfigured tunnels)
    # never leak into partner-facing payloads.
    PUBLIC_API_URL: str = ""

    @property
    def job_backoff_seconds(self) -> list[int]:
        return [int(s) for s in self.JOB_BACKOFF_SECONDS.split(",") if s.strip()]

    @property
    def job_rate_limit_backoff(self) -> list[int]:
        return [int(s) for s in self.JOB_RATE_LIMIT_BACKOFF.split(",") if s.strip()]

    @property
    def job_terminal_error_codes(self) -> set[str]:
        return {c.strip() for c in self.JOB_TERMINAL_ERROR_CODES.split(",") if c.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
