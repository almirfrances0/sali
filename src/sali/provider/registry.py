"""Provider selection — the one place the concrete backend is chosen (rule 10)."""

from __future__ import annotations

from sali.config.settings import Settings
from sali.core.errors import ConfigError
from sali.provider.base import ModelProvider
from sali.provider.fake import FakeModelProvider
from sali.provider.ollama import OllamaProvider


def build_provider(settings: Settings) -> ModelProvider:
    match settings.model.provider:
        case "ollama":
            return OllamaProvider(settings.model)
        case "fake":
            return FakeModelProvider(dim=settings.model.embed_dim)
        case other:  # pragma: no cover - guarded by the settings Literal
            raise ConfigError(f"unknown model provider: {other!r}")
