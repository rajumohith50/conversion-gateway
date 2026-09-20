"""Runtime configuration, read once from the environment.

Every variable here is documented in .env.example. Settings is a plain
pydantic-settings model constructed explicitly by whoever builds the app
(create_app, the worker, the CLI); nothing reads a global at import time, so
tests can construct one with test values and never touch the real env.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://gateway:gateway@localhost:5432/gateway"

    # One shared secret per CRM source. Empty means the source is not
    # configured, and its webhook endpoint rejects everything with 401 rather
    # than accepting unsigned traffic.
    webhook_secret_salesforce: str = ""
    webhook_secret_hubspot: str = ""

    # A webhook whose X-Webhook-Timestamp is further than this from server
    # time is rejected. Bounds the window in which a captured request could
    # be replayed, while tolerating ordinary clock skew.
    webhook_timestamp_tolerance_seconds: int = Field(default=300, ge=1)
