"""The security minimum for a server on the internet (ADR 0010): no start in production with the
secrets of .env.example, a limit on failed logins (Redis), security headers."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import identity
from app.api.main import app
from app.api.security import SECURITY_HEADERS
from app.core.config import Settings, production_problems

Factory = async_sessionmaker[AsyncSession]
BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = BACKEND_DIR.parent / ".env.example"
SETTINGS = Settings()
PASSWORD = "the-right-password"
STRONG = "q7Vb2xN9sLk4Pw1Zr8Tg5Hy3Jm6Dc0Fa"  # 32 random characters


def example_env() -> dict[str, str]:
    """The values of .env.example, passed to a server process the way deploy/docker-compose.yml does."""
    values = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MM_")}
    env.update(
        MM_ENV="prod",
        MM_DATABASE_URL=f"postgresql+asyncpg://{values['MM_DB_USER']}:{values['MM_DB_PASSWORD']}@db:5432/{values['MM_DB_NAME']}",
        MM_MQTT_USERNAME=values["MM_MQTT_BACKEND_USER"],
        MM_MQTT_PASSWORD=values["MM_MQTT_BACKEND_PASSWORD"],
        MM_SECRET_KEY=values["MM_SECRET_KEY"],
        MM_ADMIN_USERNAME=values["MM_ADMIN_USERNAME"],
        MM_ADMIN_PASSWORD=values["MM_ADMIN_PASSWORD"],
        MM_DEMO_DATA=values["MM_DEMO_DATA"],
    )
    return env


# ---------------------------------------------------------------- production start


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "-m", "app.api"],
        [sys.executable, "-m", "app.ingest"],
        [sys.executable, "-m", "app.worker"],
        [sys.executable, "-m", "alembic", "upgrade", "head"],
    ],
    ids=["api", "ingest", "worker", "migrate"],
)
def test_production_start_with_example_secrets_fails(command: list[str]) -> None:
    """Every server process of the stack refuses to start and names what to change."""
    result = subprocess.run(command, cwd=BACKEND_DIR, env=example_env(), capture_output=True, text=True, timeout=120)
    assert result.returncode == 2, result.stdout + result.stderr
    message = result.stderr
    assert "Refusing to start in production" in message
    for name in ("MM_SECRET_KEY", "MM_ADMIN_PASSWORD", "MM_DB_PASSWORD", "MM_MQTT_BACKEND_PASSWORD", "MM_DEMO_DATA"):
        assert name in message, message
    assert "make env-prod" in message
    assert "Traceback" not in message


def test_example_secrets_are_fine_for_development() -> None:
    env = example_env()
    env["MM_ENV"] = "dev"
    code = "from app.core.config import checked_settings; checked_settings(); print('started')"
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND_DIR, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "started"


def test_what_counts_as_unsafe() -> None:
    url = f"postgresql+asyncpg://mm:{STRONG}@db:5432/mm"
    safe = Settings(
        env="prod", database_url=url, secret_key=STRONG, admin_password="Ad8-min", mqtt_password=STRONG, demo_data=False
    )
    assert production_problems(safe) == []

    def problems(**changes: object) -> list[str]:
        return [p.split(":")[0] for p in production_problems(safe.model_copy(update=changes))]

    assert problems(secret_key=STRONG[:31]) == ["MM_SECRET_KEY"]  # shorter than 32
    assert problems(secret_key="change-me-" + STRONG) == ["MM_SECRET_KEY"]
    assert problems(admin_password="admin") == ["MM_ADMIN_PASSWORD"]
    assert problems(admin_password="") == ["MM_ADMIN_PASSWORD"]
    assert problems(database_url="postgresql+asyncpg://mm:change-me-db@db:5432/mm") == ["MM_DB_PASSWORD"]
    assert problems(database_url="postgresql+asyncpg://mm@db:5432/mm") == ["MM_DB_PASSWORD"]
    assert problems(mqtt_password="change-me-backend") == ["MM_MQTT_BACKEND_PASSWORD"]
    assert problems(mqtt_password=None) == ["MM_MQTT_BACKEND_PASSWORD"]
    assert problems(demo_data=True) == ["MM_DEMO_DATA"]


# ---------------------------------------------------------------- failed logins


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()


@pytest.fixture
async def accounts(session_factory: Factory, redis: Redis) -> None:
    async with session_factory() as session, session.begin():
        for login in ("alice", "bob"):
            await identity.create_user(session, login, PASSWORD, login)
    app.state.settings = SETTINGS
    app.state.session_factory = session_factory
    app.state.redis = redis


@contextlib.asynccontextmanager
async def client(ip: str = "203.0.113.10") -> AsyncIterator[httpx.AsyncClient]:
    """A browser behind the reverse proxy, which sets X-Real-IP to the address it was connected from."""
    transport = httpx.ASGITransport(app=app)
    headers = {"X-Real-IP": ip}
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1", headers=headers) as http:
        yield http


async def login(http: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    return await http.post("/auth/login", json={"username": username, "password": password})


@pytest.mark.usefixtures("accounts")
async def test_more_than_five_failures_a_minute_give_429() -> None:
    async with client() as http:
        for _ in range(5):
            assert (await login(http, "alice", "wrong")).status_code == 401
        blocked = await login(http, "alice", "wrong")
        assert blocked.status_code == 429
        assert 1 <= int(blocked.headers["Retry-After"]) <= 60
        # blocked even with the right password: guessing goes on no faster than 5 a minute
        assert (await login(http, "alice", PASSWORD)).status_code == 429
        # the pair is blocked, not the address or the login
        assert (await login(http, "bob", PASSWORD)).status_code == 200
    async with client("198.51.100.7") as other_place:
        assert (await login(other_place, "alice", PASSWORD)).status_code == 200


@pytest.mark.usefixtures("accounts")
async def test_the_window_ends_and_success_resets_the_count() -> None:
    app.state.settings = SETTINGS.model_copy(update={"login_failure_window_s": 2})
    try:
        async with client() as http:
            for _ in range(5):
                assert (await login(http, "alice", "wrong")).status_code == 401
            assert (await login(http, "alice", PASSWORD)).status_code == 429
            await asyncio.sleep(2.5)
            assert (await login(http, "alice", PASSWORD)).status_code == 200  # the window is over
            for _ in range(4):
                assert (await login(http, "alice", "wrong")).status_code == 401
            assert (await login(http, "alice", PASSWORD)).status_code == 200  # success clears the count
            for _ in range(5):
                assert (await login(http, "alice", "wrong")).status_code == 401
            assert (await login(http, "alice", "wrong")).status_code == 429
    finally:
        app.state.settings = SETTINGS


@pytest.mark.usefixtures("accounts")
async def test_without_redis_logins_still_work() -> None:
    """A cache outage must not lock everyone out: the limit is off, the rest works."""
    app.state.redis = None
    async with client() as http:
        for _ in range(8):
            assert (await login(http, "alice", "wrong")).status_code == 401
        assert (await login(http, "alice", PASSWORD)).status_code == 200


# ---------------------------------------------------------------- headers


@pytest.mark.usefixtures("accounts")
async def test_every_response_has_the_security_headers() -> None:
    async with client() as http:
        responses = [
            await http.get("/health"),  # 200
            await http.get("/machines"),  # 401: no token
            await http.get("/no-such-route"),  # 404
            await login(http, "alice", "wrong"),  # 401 of the login
        ]
    assert [r.status_code for r in responses] == [200, 401, 404, 401]
    for response in responses:
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (response.request.url, name)
    assert SECURITY_HEADERS == {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }
