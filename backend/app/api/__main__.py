"""API process: ``python -m app.api`` (uvicorn behind the reverse proxy).

The settings are checked before uvicorn starts, so in production a placeholder secret stops the
process with a short message instead of a traceback of the application startup.
"""

from __future__ import annotations

import uvicorn

from app.core.config import checked_settings


def main() -> None:
    checked_settings()
    uvicorn.run("app.api.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
