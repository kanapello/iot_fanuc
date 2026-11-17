"""Ingest process: ``python -m app.ingest``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from app.core.config import get_settings
from app.core.db import create_engine, create_session_factory
from app.core.logging import setup_logging
from app.ingest.consumer import run_forever
from app.ingest.contract import MessageValidator
from app.ingest.service import IngestService

log = logging.getLogger("app.ingest")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    engine = create_engine(settings.database_url)
    service = IngestService(create_session_factory(engine), MessageValidator(settings.schemas_dir))

    task = asyncio.create_task(run_forever(settings, service))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    log.info("ingest started")
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await engine.dispose()
    log.info("ingest stopped")


if __name__ == "__main__":
    asyncio.run(main())
