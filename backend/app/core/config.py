"""Backend settings, read from environment variables (prefix ``MM_``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MM_", extra="ignore")

    database_url: str = "postgresql+asyncpg://mm:mm@localhost:5432/mm"
    log_level: str = "INFO"
    schemas_dir: Path = _REPO_ROOT / "shared" / "schemas"

    redis_url: str = "redis://localhost:6379/0"

    # Auth (skeleton): one administrator and one shared token for kiosks / TV dashboards.
    secret_key: str = "change-me-secret"
    admin_username: str = "admin"
    admin_password: str = "admin"
    device_token: str = "change-me-device-token"
    token_ttl_hours: int = 12

    worker_interval_s: float = 2.0
    oee_interval_s: float = 10.0

    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_tls: bool = False
    mqtt_username: str | None = None
    mqtt_password: str | None = None

    # Persistent MQTT session: the broker keeps QoS 1 messages while ingest is down.
    ingest_client_id: str = "mm-ingest-1"
    ingest_share_group: str = "ingest"
    ingest_session_expiry_s: int = 7 * 24 * 3600
    ingest_batch_size: int = 500
    # Touched while the MQTT subscription is active; used by the container healthcheck.
    ingest_ready_file: Path | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
