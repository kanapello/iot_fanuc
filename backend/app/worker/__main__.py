"""Worker process: timeline, OEE and live state.

python -m app.worker                                   # run forever
python -m app.worker recalc --from <iso> [--to <iso>] [--machine <id>]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import journal
from app.admin import service as admin
from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_session_factory
from app.core.logging import setup_logging
from app.oee import service as oee
from app.timeline import service as timeline
from app.worker.live import build_live, publish_live

log = logging.getLogger("app.worker")
SessionFactory = async_sessionmaker[AsyncSession]


class Worker:
    def __init__(self, settings: Settings, session_factory: SessionFactory, redis: Redis) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._redis = redis
        self._oee_from: dict[str, datetime] = {}  # earliest change not yet reflected in OEE
        self._oee_at: dict[str, datetime] = {}
        self._published: dict[str, str] = {}

    async def tick(self, now: datetime) -> None:
        async with self._session_factory() as session:
            machines = await admin.list_machines(session)
        for machine in machines:
            machine_id = machine["id"]
            try:
                async with self._session_factory() as session, session.begin():
                    changed = await timeline.rebuild(session, machine_id, now)
                    if changed is not None:
                        self._oee_from[machine_id] = min(changed, self._oee_from.get(machine_id, changed))
                    last = self._oee_at.get(machine_id)
                    if last is None or (now - last).total_seconds() >= self._settings.oee_interval_s:
                        start = min(self._oee_from.pop(machine_id, now), now)
                        await oee.recompute(session, machine, start, now, now)
                        self._oee_at[machine_id] = now
                    payload = await build_live(session, machine, now)
                self._published[machine_id] = await publish_live(self._redis, payload, self._published.get(machine_id))
            except Exception:
                log.exception("machine tick failed", extra={"machine_id": machine_id})
        async with self._session_factory() as session, session.begin():
            await journal.close_finished_annotations(session)

    async def run(self) -> None:
        log.info("worker started", extra={"interval_s": self._settings.worker_interval_s})
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self.tick(datetime.now(UTC))
            except Exception:
                log.exception("worker tick failed")
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.2, self._settings.worker_interval_s - elapsed))


async def recalc(
    session_factory: SessionFactory, machine_id: str | None, start: datetime, end: datetime | None
) -> None:
    """Rebuild the timeline from ``start`` and OEE for [start, end].

    The timeline is one chain, so it is always rebuilt up to the present moment.
    """
    now = datetime.now(UTC)
    async with session_factory() as session:
        machines = [m for m in await admin.list_machines(session, active_only=False) if machine_id in (None, m["id"])]
    if not machines:
        raise SystemExit(f"machine not found: {machine_id}")
    for machine in machines:
        async with session_factory() as session, session.begin():
            await timeline.rebuild(session, machine["id"], now, force_from=start)
            await oee.recompute(session, machine, start - timedelta(hours=1), min(end or now, now), now)
        log.info("recalculated", extra={"machine_id": machine["id"], "from": start.isoformat()})


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def main() -> None:
    parser = argparse.ArgumentParser(prog="app.worker")
    sub = parser.add_subparsers(dest="command")
    recalc_parser = sub.add_parser("recalc", help="rebuild timeline and OEE for a period")
    recalc_parser.add_argument("--machine", default=None, help="machine id (default: all machines)")
    recalc_parser.add_argument("--from", dest="start", required=True, type=_parse_ts)
    recalc_parser.add_argument("--to", dest="end", default=None, type=_parse_ts)
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        if args.command == "recalc":
            await recalc(session_factory, args.machine or None, args.start, args.end)
            return
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        task = asyncio.create_task(Worker(settings, session_factory, redis).run())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await redis.aclose()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
