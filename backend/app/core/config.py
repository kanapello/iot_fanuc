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

    # Auth. The superadmin account is created on first start from these values.
    secret_key: str = "change-me-secret-change-me-secret-32"
    admin_username: str = "admin"
    admin_password: str = "admin"
    token_ttl_hours: int = 12
    # Development only: demo users and kiosk tokens with well-known values.
    demo_data: bool = False
    demo_password: str = "demo"

    worker_interval_s: float = 2.0
    oee_interval_s: float = 10.0
    # How far back derived data is recomputed after a change of shifts, norms, rules or reasons.
    recalc_horizon_days: int = 31
    seq_check_interval_s: float = 60.0
    seq_check_window_hours: int = 24
    collector_offline_after_s: float = 45.0
    # Largest accepted original file of a ballbar test (.b5r, PDF report), megabytes.
    geometry_max_file_mb: int = 25

    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_tls: bool = False
    mqtt_username: str | None = None
    mqtt_password: str | None = None

    # Persistent MQTT session: the broker keeps QoS 1 messages while ingest is down.
    ingest_client_id: str = "mm-ingest-1"
    # Empty = plain subscription. Mosquitto does not restore shared subscriptions after its
    # own restart, so with a share group messages published before ingest reconnects are lost.
    ingest_share_group: str = ""
    ingest_session_expiry_s: int = 7 * 24 * 3600
    ingest_batch_size: int = 500
    # Collectors and machines are cached by ingest; a moved or deleted collector is noticed within this time.
    ingest_directory_refresh_s: float = 30.0
    # Public address of the web interface for links on QR stickers, e.g. https://mm.example.com;
    # empty = taken from the request (Host / X-Forwarded-* headers of the reverse proxy).
    public_url: str | None = None
    # Touched while the MQTT subscription is active; used by the container healthcheck.
    ingest_ready_file: Path | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
