"""Digital-world adapter architecture (§15/§47) — a generic, replaceable interface to the outside world.

Sali interacts with the digital world through ADAPTERS, not hardcoded per-service special cases (§6/§18):
a browser, email, a social platform, a developer service, the Linux desktop — each is (eventually) an
adapter that advertises its capabilities and offers a uniform observe/act/verify/reconcile surface. This
module defines only the PROTOCOL + a registry so future adapters can be added without touching the
cognitive core. It deliberately ships NO concrete adapters (§18/§47: "do not implement all of them now")
— nothing here can actually use Facebook, Gmail, a browser, etc.; those are future capabilities.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DigitalAdapter(Protocol):
    """A replaceable interface to one part of the digital world. Every method is capability-level and
    mechanism-independent (§14): the planner reasons "update the profile"; the adapter decides how."""

    name: str

    def capabilities(self) -> list[str]:
        """The capability names this adapter provides (advertised via the capability registry, §33)."""
        ...

    async def discover(self) -> dict[str, Any]:
        """Inspect what this environment currently offers (§16 discovery)."""
        ...

    async def observe(self, target: str) -> dict[str, Any]:
        """Read external state for a target — evidence, before acting (§15)."""
        ...

    async def act(self, intent: str, params: dict[str, Any]) -> dict[str, Any]:
        """Perform a capability-level action. Returns a result the caller must still VERIFY (§42)."""
        ...

    async def verify(self, target: str, expected: dict[str, Any]) -> dict[str, Any]:
        """Confirm the resulting external state matches the expectation (§42) — never trust a return code."""
        ...

    async def reconcile(self, target: str) -> dict[str, Any]:
        """Compare remembered state with current external state and report the discrepancy (§22/§44)."""
        ...


class AdapterRegistry:
    """The set of digital-world adapters currently available. Adapters register their capabilities so the
    capability registry can advertise what Sali can actually reach right now — no fake advertising (§33/§34).
    Empty by default: Sali has no external adapters until one is genuinely built and registered."""

    def __init__(self) -> None:
        self._adapters: dict[str, DigitalAdapter] = {}

    def register(self, adapter: DigitalAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> DigitalAdapter | None:
        return self._adapters.get(name)

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def advertised_capabilities(self) -> dict[str, list[str]]:
        """Which real, registered adapters provide which capabilities — the honest external surface."""
        out: dict[str, list[str]] = {}
        for name, adapter in self._adapters.items():
            try:
                out[name] = list(adapter.capabilities())
            except Exception:  # noqa: BLE001 - a misbehaving adapter never breaks the registry
                out[name] = []
        return out


# The process-wide adapter registry — deliberately empty until a real adapter is built + registered.
default_adapter_registry = AdapterRegistry()
