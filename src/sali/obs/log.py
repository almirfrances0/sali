"""Structured logging via structlog. Console-friendly for the terminal today; the same
event stream becomes machine-readable JSON for the future app by swapping the renderer.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

_configured = False


# Third-party libraries that log HTTP traffic (the Ollama calls) at INFO — pure noise in the
# terminal, and they garble the input line / punch through the live animation. Keep them quiet
# unless we're explicitly at DEBUG.
_NOISY = ("httpx", "httpcore", "ollama", "urllib3", "asyncio", "asyncpg")


def configure_logging(level: str = "INFO") -> None:
    global _configured
    resolved = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=resolved)
    quiet = max(resolved, logging.WARNING)  # never below WARNING, even if the app runs at INFO
    for name in _NOISY:
        logging.getLogger(name).setLevel(quiet)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "sali", **initial: Any) -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name, **initial)  # type: ignore[no-any-return]
