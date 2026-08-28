"""Failure taxonomy (§9): classify_failure() must deterministically label exceptions, ToolResult-like
objects, and error strings so retry/backoff/escalation decisions are consistent across the runtime."""

from __future__ import annotations

from sali.core.errors import (
    ConfigError,
    DatabaseError,
    FailureClass,
    MigrationError,
    ProviderError,
    classify_failure,
)


def test_enum_values_are_stable() -> None:
    # These strings are the contract with the SQL enum `task_failure_class` (migration 0018) — keep stable.
    assert [c.value for c in FailureClass] == [
        "transient", "recoverable", "dependency", "permission", "fatal", "unknown",
    ]


def test_exception_types_map_to_classes() -> None:
    assert classify_failure(ProviderError("ollama not responding")) is FailureClass.TRANSIENT
    assert classify_failure(DatabaseError("connection to server failed")) is FailureClass.TRANSIENT
    assert classify_failure(TimeoutError()) is FailureClass.TRANSIENT
    assert classify_failure(ConnectionError()) is FailureClass.TRANSIENT
    assert classify_failure(OSError("i/o error")) is FailureClass.TRANSIENT
    assert classify_failure(PermissionError("nope")) is FailureClass.PERMISSION
    assert classify_failure(FileNotFoundError("/x")) is FailureClass.RECOVERABLE
    assert classify_failure(ValueError("bad argument")) is FailureClass.RECOVERABLE
    assert classify_failure(ConfigError("missing setting")) is FailureClass.FATAL
    assert classify_failure(MigrationError("bad sql")) is FailureClass.FATAL
    assert classify_failure(KeyError("k")) is FailureClass.FATAL
    assert classify_failure(TypeError("t")) is FailureClass.FATAL


def test_error_messages_map_to_classes() -> None:
    assert classify_failure("Permission denied") is FailureClass.PERMISSION
    assert classify_failure("bind: operation not permitted") is FailureClass.PERMISSION
    assert classify_failure("connection refused") is FailureClass.TRANSIENT
    assert classify_failure("Read timed out") is FailureClass.TRANSIENT
    assert classify_failure("nginx: command not found") is FailureClass.RECOVERABLE
    assert classify_failure("database already exists") is FailureClass.RECOVERABLE
    assert classify_failure("this step depends on step 3 completing") is FailureClass.DEPENDENCY
    assert classify_failure("something entirely novel happened") is FailureClass.UNKNOWN


def test_precedence_permission_beats_recoverable() -> None:
    # A message with both signals should take the more-consequential (never-auto-retry) class.
    assert classify_failure("sudo: command not found") is FailureClass.PERMISSION


def test_toolresult_like_is_duck_typed_without_importing_tools() -> None:
    class ResultLike:  # mirrors ToolResult's shape; core must not import the tools layer
        def __init__(self, error: str) -> None:
            self.error = error
            self.ok = False

    assert classify_failure(ResultLike("permission denied")) is FailureClass.PERMISSION
    assert classify_failure(ResultLike("connection reset by peer")) is FailureClass.TRANSIENT
    assert classify_failure(ResultLike("apt: package not found")) is FailureClass.RECOVERABLE


def test_non_failures_are_unknown() -> None:
    assert classify_failure(None) is FailureClass.UNKNOWN
    assert classify_failure(42) is FailureClass.UNKNOWN
    assert classify_failure("") is FailureClass.UNKNOWN
