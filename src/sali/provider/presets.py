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
# WHEN ALMIR IS TELLING SALI ABOUT HIS LIFE.
#
# Not a mood setting — a correction to the wrong tool being used for the job. Every conversational
# turn ran at CREATIVE, which is right for open-ended talk and wrong here for two measurable reasons.
#
# TEMPERATURE. Told "i lost 2 of my legs and 1 hand", the highest-probability human continuation is
# some form of "I'm so sorry". At 0.8 that is merely the most likely of many, and what actually came
# out was "Wow 😳 ... How old did you say you were? You're barely twenty-two" — a novel opener and an
# invented fact. Sampling for novelty is exactly wrong when the ordinary thing is the right thing,
# and the same knob that reaches for a surprising word reaches for a fact he does not have.
#
# REPEAT PENALTY. 1.1 charges the model for reusing tokens — including ALMIR'S OWN WORDS. Reflecting
# what someone just told you back to them is most of what listening sounds like, and this taxed it.
#
# Deliberately NOT DETERMINISTIC: seed-pinned sampling is what produced the identical-reply-every-time
# behaviour a previous audit had to unpick, and a companion who answers grief with a canned sentence
# is its own failure. Low, not zero.
TENDER = SamplingPreset(
    temperature=0.35, top_k=40, top_p=0.9, presence_penalty=0.0, repeat_penalty=1.02
)
