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
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import service as admin
from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_session_factory
from app.core.logging import setup_logging
from app.ingest.health import find_seq_gaps
from app.oee import service as oee
from app.oee.dirty import mark_oee_dirty
from app.timeline import service as timeline
from app.worker.live import build_live, publish_live

log = logging.getLogger("app.worker")
SessionFactory = async_sessionmaker[AsyncSession]


class Worker:
    """Periodic derivation of timeline, OEE and live state.

    Nothing about pending work is kept in memory: "what must be recomputed"
    lives in timeline_dirty / oee_dirty, so a restart at any moment loses nothing.
    """

    def __init__(self, settings: Settings, session_factory: SessionFactory, redis: Redis | None = None) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._redis = redis
        self._oee_at: dict[str, datetime] = {}  # last refresh of the current hour / shift (only a throttle)
        self._published: dict[str, str] = {}
        self._gaps_at: datetime | None = None
        self._known_gaps: set[tuple[str, int, int]] = set()

    async def rebuild_timeline(self, machine: dict[str, Any], now: datetime) -> None:
        """Timeline step. A change is recorded as an OEE mark in the same transaction."""
        async with self._session_factory() as session, session.begin():
            changed = await timeline.rebuild(session, machine["id"], now)
            if changed is not None:
                await mark_oee_dirty(session, {machine["id"]: changed})

    async def update_oee(self, machine: dict[str, Any], now: datetime) -> None:
        """OEE step: pending periods first, then the running hour and shift."""
        machine_id = machine["id"]
        async with self._session_factory() as session, session.begin():
            await oee.process_dirty(session, machine, now)
        last = self._oee_at.get(machine_id)
        if last is None or (now - last).total_seconds() >= self._settings.oee_interval_s:
            async with self._session_factory() as session, session.begin():
                await oee.recompute(session, machine, now, now, now)
            self._oee_at[machine_id] = now

    async def publish(self, machine: dict[str, Any], now: datetime) -> None:
        if self._redis is None:
            return
        async with self._session_factory() as session:
            payload = await build_live(session, machine, now)
        machine_id = machine["id"]
        self._published[machine_id] = await publish_live(self._redis, payload, self._published.get(machine_id))

    async def check_seq_gaps(self, now: datetime) -> None:
        """Log every newly found gap in message sequence numbers (lost data)."""
        if self._gaps_at is not None and (now - self._gaps_at).total_seconds() < self._settings.seq_check_interval_s:
            return
        self._gaps_at = now
        async with self._session_factory() as session:
            gaps = await find_seq_gaps(session, now, self._settings.seq_check_window_hours)
        for gap in gaps:
            key = (gap["machine_id"], gap["after_seq"], gap["before_seq"])
            if key not in self._known_gaps:
                self._known_gaps.add(key)
                log.warning("seq gap: messages lost", extra=gap)

    async def tick(self, now: datetime) -> None:
        async with self._session_factory() as session:
            machines = await admin.list_machines(session)
        for machine in machines:
            try:
                await self.rebuild_timeline(machine, now)
                await self.update_oee(machine, now)
                await self.publish(machine, now)
            except Exception:
                log.exception("machine tick failed", extra={"machine_id": machine["id"]})
        await self.check_seq_gaps(now)

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
    """Rebuild timeline and OEE for [start, end]; without ``end`` up to the present moment.

    Intervals after ``end`` are left untouched and stitched at the border.
    """
    now = datetime.now(UTC)
    async with session_factory() as session:
        machines = [m for m in await admin.list_machines(session, active_only=False) if machine_id in (None, m["id"])]
    if not machines:
        raise SystemExit(f"machine not found: {machine_id}")
    stop = min(end or now, now)
    for machine in machines:
        cursor = start
        while cursor < stop:  # one day per transaction
            chunk_end = min(cursor + timeline.CHUNK, stop)
            async with session_factory() as session, session.begin():
                await timeline.rebuild(session, machine["id"], now, force_from=cursor, force_to=chunk_end)
                await oee.recompute(session, machine, cursor, chunk_end, now)
            cursor = chunk_end
        log.info("recalculated", extra={"machine_id": machine["id"], "from": start.isoformat(), "to": stop.isoformat()})


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
        redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
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
