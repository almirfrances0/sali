"""Sampling presets.

Ollama merges request options *over* the Modelfile's PARAMETERs, so any knob we omit
inherits the model's baked-in default (for ``sali:latest`` that is temperature=1,
presence_penalty=1.5 — poison for machine-parsed output). Every preset therefore pins
*every* knob explicitly (engineering fix H6): the DETERMINISTIC block must be sent on all
tool/JSON turns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SamplingPreset:
    temperature: float
    top_k: int
    top_p: float
    presence_penalty: float
    repeat_penalty: float
    seed: int | None = None

    def to_options(self) -> dict[str, Any]:
        """A complete, explicit Ollama options block (never a partial override)."""
        opts: dict[str, Any] = {
            "temperature": self.temperature,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "presence_penalty": self.presence_penalty,
            "repeat_penalty": self.repeat_penalty,
        }
        if self.seed is not None:
            opts["seed"] = self.seed
        return opts


# Mandatory for every machine-parsed (tool/JSON/extractor) call.
DETERMINISTIC = SamplingPreset(
    temperature=0.0, top_k=1, top_p=1.0, presence_penalty=0.0, repeat_penalty=1.0, seed=42
)
# Summaries / episode distillation.
BALANCED = SamplingPreset(
    temperature=0.4, top_k=40, top_p=0.95, presence_penalty=0.0, repeat_penalty=1.1
)
# Open-ended conversation.
CREATIVE = SamplingPreset(
    temperature=0.8, top_k=40, top_p=0.95, presence_penalty=0.0, repeat_penalty=1.1
)
