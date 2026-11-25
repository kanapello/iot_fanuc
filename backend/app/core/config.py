"""Backend settings, read from environment variables (prefix ``MM_``)."""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

_REPO_ROOT = Path(__file__).resolve().parents[3]
PLACEHOLDER = "change-me"  # every secret in .env.example starts with it
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MM_", extra="ignore")

    # "prod": the server refuses to start with the secrets of .env.example (ADR 0010)
    env: Literal["dev", "prod"] = "dev"
    database_url: str = "postgresql+asyncpg://mm:mm@localhost:5432/mm"
    log_level: str = "INFO"
    schemas_dir: Path = _REPO_ROOT / "shared" / "schemas"

    redis_url: str = "redis://localhost:6379/0"

    # Auth. The superadmin account is created on first start from these values.
    secret_key: str = "change-me-secret-change-me-secret-32"
    admin_username: str = "admin"
    admin_password: str = "admin"
    token_ttl_hours: int = 12
    # More failed logins than this within the window from one address for one login: 429 (Redis)
    login_max_failures: int = 5
    login_failure_window_s: int = 60
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

    # Telegram notifications (ADR 0011). Without a token nothing is sent; the bot name builds the
    # link t.me/<bot>?start=<code> of the profile page.
    telegram_bot_token: str | None = None
    telegram_bot_username: str | None = None
    telegram_api_url: str = "https://api.telegram.org"
    # The same event of the same subject is announced at most once in this many minutes.
    notify_repeat_minutes: int = 15
    notify_interval_s: float = 10.0
    collector_offline_notify_minutes: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()


def production_problems(settings: Settings) -> list[str]:
    """What makes these settings unfit for a server open to the internet; empty when they are fine."""
    problems = []
    if settings.secret_key.startswith(PLACEHOLDER) or len(settings.secret_key) < MIN_SECRET_KEY_LENGTH:
        problems.append(
            f"MM_SECRET_KEY: a placeholder from .env.example or shorter than {MIN_SECRET_KEY_LENGTH} characters"
        )
    if settings.admin_password in ("", "admin"):
        problems.append('MM_ADMIN_PASSWORD: empty or "admin"')
    db_password = make_url(settings.database_url).password
    if not db_password or str(db_password).startswith(PLACEHOLDER):
        problems.append(f"MM_DB_PASSWORD: empty or a placeholder from .env.example ({PLACEHOLDER}...)")
    if not settings.mqtt_password or settings.mqtt_password.startswith(PLACEHOLDER):
        problems.append(f"MM_MQTT_BACKEND_PASSWORD: empty or a placeholder from .env.example ({PLACEHOLDER}...)")
    if settings.demo_data:
        problems.append("MM_DEMO_DATA: must be false (demo accounts have well-known passwords)")
    return problems


def checked_settings() -> Settings:
    """Settings of a server process. In production unsafe values stop the start with a clear message."""
    settings = get_settings()
    problems = production_problems(settings) if settings.env == "prod" else []
    if problems:
        lines = [
            "Refusing to start in production (MM_ENV=prod): these settings are unsafe:",
            *(f"  - {problem}" for problem in problems),
            "Create .env with random passwords: make env-prod (see docs/runbooks/cloud-install.md).",
        ]
        print("\n".join(lines), file=sys.stderr, flush=True)
        raise SystemExit(2)
    return settings
