import os
import socket

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App2 settings, read from APP2_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="APP2_", extra="ignore")

    database_url: str = "postgresql://app2:app2@localhost:5432/integration"
    db_pool_max: int = 10
    target_app: str = "app2"
    worker_id: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    worker_host: str = Field(default_factory=socket.gethostname)
    worker_enabled: bool = True
    max_concurrency: int = 5              # requests processed in parallel
    lease_seconds: int = 120              # extended by heartbeats while processing
    poll_seconds: float = 5               # fallback when no NOTIFY arrives
    schema_cache_seconds: float = 60
