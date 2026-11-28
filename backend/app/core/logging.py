"""Structured JSON logging."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


_TOKEN = re.compile(r"(token=)[^&\s\"']+")


class MaskTokens(logging.Filter):
    """Hides ``token=...`` in log records: kiosks, TV dashboards and every /ws/live connection carry a token
    in the query of the address, and uvicorn writes the address of each request and WebSocket."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TOKEN.sub(r"\1***", record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_TOKEN.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        return True


def mask_tokens(*logger_names: str) -> None:
    for name in logger_names:
        logger = logging.getLogger(name)
        if not any(isinstance(f, MaskTokens) for f in logger.filters):
            logger.addFilter(MaskTokens())
