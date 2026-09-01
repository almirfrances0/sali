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
    root = logging.getLogger()
    # Purge all handlers and reconfigure from scratch. Without this, cached handlers
    # from an earlier basicConfig call keep emitting even after the level is raised.
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setLevel(resolved)
    root.addHandler(handler)
    root.setLevel(resolved)
    quiet = max(resolved, logging.WARNING)
    for name in _NOISY:
        logging.getLogger(name).setLevel(quiet)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(resolved),
        cache_logger_on_first_use=False,
    )
    _configured = True


def get_logger(name: str = "sali", **initial: Any) -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure_logging("WARNING")
    return structlog.get_logger(name, **initial)  # type: ignore[no-any-return]
