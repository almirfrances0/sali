"""THE ONE INFERENCE AUTHORITY — the single gate every model call passes through.

Sali reasons with one model on one 12 GB card. Two processes reasoning at once is not "more
throughput"; it is two minds, twice the VRAM pressure, and (historically, on this machine) a
hard power-off. So *cognition* — the heavy `sali:latest` model thinking — is a privilege of the
one authoritative runtime, and this module is where that privilege is checked.

Two kinds of model call, deliberately treated differently:

* ``COGNITION`` — chat, streaming chat, vision. This is Sali *thinking*. It runs the 35B model
  split across GPU and CPU. Only the living mind may do it. A process that is not the mind while
  a mind is alive is refused here, loudly, rather than quietly opening a second reasoning path.
* ``EMBEDDING`` — the small `nomic-embed-text` model, pinned to the CPU (``num_gpu: 0``). It is a
  *data* operation (indexing a memory, ranking a recall), not thought, and it touches no VRAM. It
  stays available to utility commands so `sali remember` / `sali recall` keep working while Sali
  lives. It is still serialised by the machine-wide inference lease, so it can never pile CPU load
  onto a generation already overflowing onto the CPU.

Why refuse instead of queue: waiting would make a second mind *work*, just slowly — and the class
of bug being eliminated is exactly "it worked, so nobody noticed there were two". A refusal names
the mistake at the moment it is made.

When *no* mind is alive, a lone process is allowed to reason: it is then the only Sali on the
machine, which is the same invariant seen from the other side. Nothing here weakens that — the
machine-wide flock in :mod:`sali.provider.ollama` still guarantees one generation at a time.
"""

from __future__ import annotations

from enum import StrEnum

from sali.core.errors import ProviderError
from sali.core.mind import (
    MindHolder,
    ProcessRole,
    current_role,
    describe_holder,
    held_by_this_process,
    live_holder,
)

__all__ = ["InferenceKind", "InferenceRefused", "admit", "explain", "may_run_cognition"]


class InferenceKind(StrEnum):
    COGNITION = "cognition"    # the heavy reasoning model — the mind's alone
    EMBEDDING = "embedding"    # CPU-only vectors — a data operation, open to utilities


class InferenceRefused(ProviderError):
    """A non-authoritative process tried to run cognition while the one mind is alive."""

    def __init__(self, holder: MindHolder | None, kind: InferenceKind) -> None:
        self.holder = holder
        self.kind = kind
        super().__init__(
            f"Refused {kind.value}: this process is not Sali's mind, and Sali is alive. "
            f"{describe_holder(holder)} Cognition belongs to the one runtime — reach him through "
            "it (`sali agent`, the iPhone app, or the API) instead of starting a second reasoning "
            "path. Only embeddings and datastore work are available to a non-mind process.")


def may_run_cognition() -> tuple[bool, MindHolder | None]:
    """(allowed, the live mind). Allowed iff we ARE the mind, or no mind exists to be second to."""
    if held_by_this_process():
        return True, None
    holder = live_holder()
    return holder is None, holder


def admit(kind: InferenceKind) -> None:
    """The gate. Raises :class:`InferenceRefused` when this call would open a second mind's mouth."""
    if kind is InferenceKind.EMBEDDING:
        return
    allowed, holder = may_run_cognition()
    if not allowed:
        raise InferenceRefused(holder, kind)


def explain() -> dict[str, object]:
    """Diagnostics for `sali status` / `sali doctor`: who may think, and why."""
    allowed, holder = may_run_cognition()
    return {
        "process_role": current_role().value,
        "is_mind": current_role() is ProcessRole.MIND and held_by_this_process(),
        "mind_alive": holder is not None or held_by_this_process(),
        "mind": holder.to_dict() if holder is not None else None,
        "cognition_allowed_here": allowed,
        "embeddings_allowed_here": True,
    }
