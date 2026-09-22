"""Runtime configuration, read once from the environment.

Every variable here is documented in .env.example. Settings is a plain
pydantic-settings model constructed explicitly by whoever builds the app
(create_app, the worker, the CLI); nothing reads a global at import time, so
tests can construct one with test values and never touch the real env.
"""

from typing import Literal

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

    # "memory" or "pubsub". Memory is one process only (the API and worker
    # must share the object), so it is for tests and demos; pubsub is for
    # anything with two processes.
    queue_backend: Literal["memory", "pubsub"] = "pubsub"
    pubsub_project_id: str = "local-project"
    pubsub_topic: str = "lead-events"
    pubsub_subscription: str = "lead-events-processor"
    # Read by the google-cloud-pubsub client itself, not by our code; listed
    # here so Settings documents every variable the process depends on.
    pubsub_emulator_host: str = ""

    # Worker tuning.
    worker_batch_size: int = Field(default=10, ge=1)
    worker_poll_timeout_seconds: float = Field(default=5.0, gt=0)

    # reconcile: a QUEUED event older than this with no progress is stuck.
    reconcile_stuck_after_seconds: int = Field(default=300, ge=1)

    # Upload client. Base URL points at the mock locally.
    ads_api_base_url: str = "http://localhost:8081"
    ads_api_token: str = ""
    ads_api_timeout_seconds: float = Field(default=10.0, gt=0)
    # Conversion actions are addressed as customers/<id>/conversionActions/<name>.
    ads_customer_id: str = "1234567890"

    # Batching: send when this many rows are waiting, or when the oldest
    # has waited this long.
    upload_batch_size: int = Field(default=100, ge=1)
    upload_batch_wait_seconds: float = Field(default=5.0, ge=0)

    # Retry policy for transient failures (design section 7).
    upload_max_attempts: int = Field(default=6, ge=1)
    upload_max_elapsed_seconds: float = Field(default=600.0, gt=0)
    upload_backoff_base_seconds: float = Field(default=1.0, gt=0)
    upload_backoff_max_seconds: float = Field(default=60.0, gt=0)
    # Row-level retryable errors are re-claimed after this long; a row
    # stuck in UPLOADING longer than stale_after is assumed orphaned.
    upload_row_retry_after_seconds: int = Field(default=60, ge=0)
    upload_stale_after_seconds: int = Field(default=600, ge=1)
    upload_poll_interval_seconds: float = Field(default=1.0, gt=0)

    # Observability.
    log_level: str = "INFO"
    # Worker and uploader processes serve their Prometheus registry here;
    # the API serves its own at /metrics on its normal port.
    metrics_port: int = Field(default=9090, ge=1, le=65535)
