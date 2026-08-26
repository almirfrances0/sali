from __future__ import annotations

import pytest

from sali.config.settings import Settings
from sali.provider.presets import DETERMINISTIC


def test_defaults() -> None:
    s = Settings()
    assert s.model.chat_model == "sali:latest"
    assert s.model.provider == "ollama"
    assert s.db.user == "almir"
    assert s.db.host == "/var/run/postgresql"  # peer auth, no password


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALI_MODEL__CHAT_MODEL", "other:1b")
    monkeypatch.setenv("SALI_RUNTIME__MAX_ITERATIONS", "3")
    s = Settings()
    assert s.model.chat_model == "other:1b"
    assert s.runtime.max_iterations == 3


def test_deterministic_preset_pins_every_knob() -> None:
    # fix H6: nothing is left to the model's poisoned Modelfile defaults.
    o = DETERMINISTIC.to_options()
    assert o["temperature"] == 0.0
    assert o["top_k"] == 1
    assert o["top_p"] == 1.0
    assert o["presence_penalty"] == 0.0
    assert o["repeat_penalty"] == 1.0
    assert o["seed"] == 42
