"""Sali's exception hierarchy + failure taxonomy. Errors are never hidden (engineering rule 12)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class SaliError(Exception):
    """Base class for all Sali errors."""


class ConfigError(SaliError):
    """Invalid or missing configuration."""


class DatabaseError(SaliError):
    """Datastore connectivity or integrity failure."""


class MigrationError(DatabaseError):
    """A schema migration could not be applied."""


class ProviderError(SaliError):
    """Model provider (Ollama, etc.) failure."""


# ── Failure taxonomy (spec §9) ────────────────────────────────────────────────────────────────────
# A single, shared vocabulary for *why* something failed, so retry/backoff/escalation decisions are
# consistent instead of ad-hoc at each call site. Derived DETERMINISTICALLY from an exception, a
# ToolResult-like object, or an error string — never from the model. Mirrors the SQL enum
# `task_failure_class` (kept in lockstep; see migration 0018). The retry driver (§3-6/§38) reads this:
# TRANSIENT/RECOVERABLE → bounded auto-retry; DEPENDENCY → block on a prerequisite; PERMISSION → needs
# authority/user (never auto-retry); FATAL → stop and surface; UNKNOWN → surface, do not blindly retry.
class FailureClass(StrEnum):
    TRANSIENT = "transient"      # retry as-is: timeout, connection reset, service temporarily down
    RECOVERABLE = "recoverable"  # retry after a fix: missing binary, bad args, resolvable state
    DEPENDENCY = "dependency"    # blocked on a prerequisite step/resource being done first
    PERMISSION = "permission"    # needs privilege / authority / the user — do NOT auto-retry
    FATAL = "fatal"              # logic / unrecoverable — stop and surface
    UNKNOWN = "unknown"          # unclassifiable — surface honestly, never blindly retry


_PERMISSION = (
    "permission denied", "not permitted", "operation not permitted", "must be root", "sudo",
    "unauthor", "forbidden", "access denied", "eacces", "eperm",
)
_TRANSIENT = (
    "timed out", "timeout", "connection refused", "connection reset", "reset by peer",
    "temporarily unavailable", "try again", "broken pipe", "network is unreachable", "no route to host",
    "connection aborted", "econnreset", "etimedout", "server disconnected", "read timed out",
    "service unavailable", "503 ", "502 ", "429 ",
)
_DEPENDENCY = ("depends on", "prerequisite", "must first", "requires ", "blocked by")
_RECOVERABLE = (
    "command not found", "no such file", "no such directory", "not found", "does not exist",
    "already exists", "not installed", "missing", "cannot stat", "invalid argument",
)


def _classify_message(msg: str) -> FailureClass:
    m = msg.lower()
    if any(s in m for s in _PERMISSION):
        return FailureClass.PERMISSION
    if any(s in m for s in _TRANSIENT):
        return FailureClass.TRANSIENT
    if any(s in m for s in _DEPENDENCY):
        return FailureClass.DEPENDENCY
    if any(s in m for s in _RECOVERABLE):
        return FailureClass.RECOVERABLE
    return FailureClass.UNKNOWN


def _classify_exception(exc: BaseException) -> FailureClass | None:
    if isinstance(exc, ConfigError | MigrationError):
        return FailureClass.FATAL
    if isinstance(exc, PermissionError):
        return FailureClass.PERMISSION
    if isinstance(exc, FileNotFoundError):
        return FailureClass.RECOVERABLE
    if isinstance(exc, ValueError):
        return FailureClass.RECOVERABLE  # usually malformed input the caller can correct
    if isinstance(exc, TimeoutError | ConnectionError | ProviderError | DatabaseError):
        return FailureClass.TRANSIENT
    if isinstance(exc, TypeError | KeyError | AttributeError | IndexError | AssertionError):
        return FailureClass.FATAL  # a programming/logic error, not a retryable condition
    if isinstance(exc, OSError):
        return FailureClass.TRANSIENT  # generic OS/IO — often retryable
    return None  # unknown exception type → fall through to message heuristics


def classify_failure(x: Any) -> FailureClass:
    """Deterministically label a failure (§9). Accepts an exception, a ToolResult-like object
    (duck-typed via ``.error``), or an error string. Pure — no side effects, no I/O."""
    err = getattr(x, "error", None)
    if err is not None and not isinstance(x, BaseException):
        return _classify_message(str(err))  # ToolResult-like
    if isinstance(x, BaseException):
        return _classify_exception(x) or _classify_message(str(x))
    if isinstance(x, str):
        return _classify_message(x)
    return FailureClass.UNKNOWN
