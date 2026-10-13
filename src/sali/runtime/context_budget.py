"""Deterministic context-window budgeting (Prompt 3, §7/§27/§28).

The LLM context is DISPOSABLE; the task state is DURABLE. This module owns the proactive side of
that principle: it measures how full the working prompt is against the model's real context window
and decides — deterministically, before the provider is ever asked — when to compact. It never asks
the model how much room is left; it computes it.

The window comes from the provider where possible (``provider.context_limit()``), falling back to the
configured ``ctx_default``/``ctx_max`` so a backend that can't report a limit is still bounded and
observable rather than silently assumed infinite (§28). Output tokens are reserved off the top so the
model always has room to finish a reasoning step (§7).

Duck-typed on the provider (only ``count_tokens`` / optional ``context_limit`` are used) and on the
message shape (``.content``) so this stays dependency-light and provider-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# Fractions of the USABLE budget (limit − reserved output) at which each state begins. _COMPACT keeps
# the historical 0.88 fold point (loop._CTX_HEADROOM) so behaviour is preserved: we still fold well
# before the hard wall, just now with an explicit output reservation and a named status.
_APPROACHING = 0.75
_COMPACT = 0.88
# Reserve enough output room that a reasoning step (and its tool calls) can always be emitted (§7).
_RESERVE_OUTPUT = 2048
# Per-message framing overhead the raw char-count heuristic misses, plus a pad on the token estimate
# so the budget is a conservative UPPER bound (never an under-count that lets us overflow). Mirrors
# the loop's prior _CTX_MARGIN / ×1.3 so the fold point is unchanged.
_PER_MESSAGE_OVERHEAD = 8
_COUNT_PAD = 1.3
# Last-resort window when neither the provider nor settings can name one — small on purpose, so an
# unknown backend compacts EARLY rather than optimistically overflowing.
_FALLBACK_LIMIT = 8192

# Substrings that mark a provider error as "the request was too large for the context window", so the
# runtime can compact-and-retry instead of failing the task (§8). Matched case-insensitively against
# the whole error string; kept broad because different backends word this very differently.
_OVERFLOW_MARKERS = (
    "context length", "context window", "context_length", "maximum context", "max context",
    "context limit", "too many tokens", "token limit", "tokens exceed", "exceeds the maximum",
    "exceeds context", "input is too large", "prompt is too long", "prompt too long",
    "request too large", "reduce the length", "num_ctx", "413", "context_length_exceeded",
    "out of context", "exceeded the context",
)


class ContextStatus(StrEnum):
    """How full the working prompt is, as a deterministic four-state ladder (§7)."""

    SAFE = "safe"                          # plenty of room — carry on
    APPROACHING_LIMIT = "approaching_limit"  # getting full — surface it, keep going
    COMPACT_REQUIRED = "compact_required"    # fold BEFORE the next model call
    EMERGENCY = "emergency"                  # at/over the wall — fold aggressively


@dataclass(slots=True, frozen=True)
class ContextBudget:
    """A snapshot of context pressure: the window, what's used, and what to do about it."""

    limit: int
    used: int
    reserved_output: int
    status: ContextStatus

    @property
    def usable(self) -> int:
        """Tokens available for the prompt once output room is reserved."""
        return max(1, self.limit - self.reserved_output)

    @property
    def remaining(self) -> int:
        return max(0, self.usable - self.used)

    @property
    def fraction(self) -> float:
        return self.used / self.usable

    @property
    def is_approaching(self) -> bool:
        return self.status in (
            ContextStatus.APPROACHING_LIMIT, ContextStatus.COMPACT_REQUIRED, ContextStatus.EMERGENCY,
        )

    @property
    def should_compact(self) -> bool:
        return self.status in (ContextStatus.COMPACT_REQUIRED, ContextStatus.EMERGENCY)

    @property
    def is_emergency(self) -> bool:
        return self.status is ContextStatus.EMERGENCY

    def as_dict(self) -> dict[str, Any]:
        """Structured observability payload (§33) — never any prompt/tool content, only counts."""
        return {
            "limit": self.limit, "used": self.used, "remaining": self.remaining,
            "reserved_output": self.reserved_output, "status": self.status.value,
            "fraction": round(self.fraction, 3),
        }


def estimate_tokens(provider: Any, messages: list[Any]) -> int:
    """A conservative UPPER bound on the tokens a message list will cost (§7). Over-counts on purpose."""
    return sum(
        int(provider.count_tokens(m.content or "") * _COUNT_PAD) + _PER_MESSAGE_OVERHEAD
        for m in messages
    )


def resolve_limit(provider: Any, settings: Any = None) -> int:
    """Sali's EFFECTIVE OPERATIONAL context budget (Prompt 6 §3): the SMALLER of the model's physical
    window and Sali's configured operational limit. The physical window is the provider's own limit (or
    the configured model window); Sali deliberately works in a small, conservative budget (~24K) so the
    KV-cache/VRAM/latency stay low, using durable state as the real memory — never silently unbounded."""
    physical: Any = None
    try:
        get = getattr(provider, "context_limit", None)
        if callable(get):
            physical = get()
    except Exception:  # noqa: BLE001 - a provider that can't report a limit falls back, never crashes
        physical = None
    if not (isinstance(physical, int) and physical > 0):
        physical = None
    if physical is None and settings is not None:
        try:
            physical = int(min(settings.model.ctx_default, settings.model.ctx_max))
        except Exception:  # noqa: BLE001
            physical = None
    if physical is None:
        physical = _FALLBACK_LIMIT
    # Cap by Sali's configured operational budget when set — the small working set the model runs in.
    if settings is not None:
        try:
            operational = int(settings.runtime.context_limit)
            if operational > 0:
                return min(int(physical), operational)
        except Exception:  # noqa: BLE001
            pass
    return int(physical)


def output_reserve(settings: Any = None) -> int:
    """The tokens always kept free for the model's reply (§18) — configurable, else the default."""
    if settings is not None:
        try:
            r = int(settings.runtime.output_reserve)
            if r > 0:
                return r
        except Exception:  # noqa: BLE001
            pass
    return _RESERVE_OUTPUT


def assess(
    provider: Any, messages: list[Any], *, limit: int, reserved_output: int = _RESERVE_OUTPUT,
) -> ContextBudget:
    """Classify context pressure for ``messages`` against ``limit`` (deterministic, no I/O, no model)."""
    used = estimate_tokens(provider, messages)
    usable = max(1, limit - reserved_output)
    frac = used / usable
    if frac >= 1.0:
        status = ContextStatus.EMERGENCY
    elif frac >= _COMPACT:
        status = ContextStatus.COMPACT_REQUIRED
    elif frac >= _APPROACHING:
        status = ContextStatus.APPROACHING_LIMIT
    else:
        status = ContextStatus.SAFE
    return ContextBudget(limit=limit, used=used, reserved_output=reserved_output, status=status)


def budget_for(
    provider: Any, settings: Any, messages: list[Any], *, reserved_output: int | None = None,
) -> ContextBudget:
    """Resolve the operational limit + output reserve from settings and assess ``messages`` against it."""
    reserve = reserved_output if reserved_output is not None else output_reserve(settings)
    return assess(provider, messages, limit=resolve_limit(provider, settings),
                  reserved_output=reserve)


def is_context_overflow(x: Any) -> bool:
    """True when a provider error (or its message) means the request exceeded the context window (§8).

    Broad + case-insensitive: different backends word this very differently, and a false positive only
    costs one extra compaction (bounded), whereas a false negative would fail a resumable task."""
    text = str(x).lower()
    return any(marker in text for marker in _OVERFLOW_MARKERS)
