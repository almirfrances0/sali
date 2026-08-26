"""Layered settings: runtime-overlay > env (SALI_) > TOML (~/.config/sali/sali.toml) > defaults.

Peer authentication over the unix socket means there is deliberately no database password
anywhere in this configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

TOML_PATH = Path.home() / ".config" / "sali" / "sali.toml"


class DbSettings(BaseModel):
    host: str = "/var/run/postgresql"  # unix socket dir -> peer auth
    port: int = 5432
    user: str = "almir"
    name: str = "sali"
    pool_min: int = 2
    pool_max: int = 8
    statement_timeout_ms: int = 30_000


class ModelSettings(BaseModel):
    provider: Literal["ollama", "fake"] = "ollama"
    host: str = "http://localhost:11434"
    chat_model: str = "sali:latest"  # Sali's single reasoning backend
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    embed_num_gpu: int = 0  # CPU-only embeddings — never contend with the model for VRAM
    # Roomy context so long writes/tasks aren't truncated. Larger = slower (KV cache spills to
    # CPU under 12 GB VRAM), which Almir explicitly accepts: "he can do slowly" over "has limits".
    ctx_default: int = 16_384
    ctx_max: int = 32_768
    request_timeout_s: float = 300.0  # long single-shot generations must not time out mid-page


class RuntimeSettings(BaseModel):
    max_iterations: int = 20
    token_budget_per_run: int = 120_000


def _default_fs_deny() -> list[str]:
    # This machine is Sali's home — it's open to it. Only credentials and the separate
    # `salix` project stay protected by default (a light guard the owner can remove).
    h = Path.home()
    return [
        str(h / ".ssh"), str(h / ".gnupg"), str(h / ".aws"),
        str(h / ".git-credentials"), str(h / ".netrc"), str(h / "Desktop" / "salix"),
    ]


class PermissionsSettings(BaseModel):
    # Broad read (Sali reads its own source, configs, logs) and broad write (its whole home).
    fs_read_roots: list[str] = Field(
        default_factory=lambda: [str(Path.home()), "/etc", "/usr", "/proc", "/var/log"]
    )
    fs_write_roots: list[str] = Field(default_factory=lambda: [str(Path.home())])
    fs_deny: list[str] = Field(default_factory=_default_fs_deny)
    exec_cwd: str = Field(default_factory=lambda: str(Path.home()))
    exec_allow_network: bool = True  # Sali runs freely, including networked commands (installs)
    jail_learning: bool = True  # an isolated sandbox is available for learning-time experiments


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SALI_",
        env_nested_delimiter="__",
        extra="ignore",
        toml_file=TOML_PATH,
    )

    log_level: str = "INFO"
    db: DbSettings = Field(default_factory=DbSettings)
    model: ModelSettings = Field(default_factory=ModelSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    permissions: PermissionsSettings = Field(default_factory=PermissionsSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority order: earlier sources win.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


def load_settings(**overrides: Any) -> Settings:
    """Build settings, applying any explicit runtime overrides at the highest priority."""
    return Settings(**overrides)
