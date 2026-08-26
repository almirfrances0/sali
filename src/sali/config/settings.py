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
    ctx_default: int = 8192
    ctx_max: int = 32_768
    request_timeout_s: float = 120.0


class RuntimeSettings(BaseModel):
    max_iterations: int = 8
    token_budget_per_run: int = 24_000


def _default_fs_deny() -> list[str]:
    h = Path.home()
    return [
        str(h / ".ssh"), str(h / ".gnupg"), str(h / ".aws"), str(h / ".config" / "sali"),
        str(h / "Desktop" / "salix"), str(h / ".bash_history"), str(h / ".zsh_history"),
        str(h / ".netrc"), str(h / ".git-credentials"), "/etc/shadow", "/etc/gshadow",
    ]


def _default_exec_allowlist() -> list[str]:
    # ONLY binaries that cannot execute further code from their arguments. Deliberately
    # excludes git / systemctl / journalctl (they take config/alias/-c args that spawn
    # subprocesses) — those have dedicated, scoped tools instead. Interpreters are refused too.
    return [
        "uname", "uptime", "df", "free", "lscpu", "lsblk", "nproc", "ip", "ss", "ps", "who",
        "id", "date", "nvidia-smi", "stat", "du", "lsof", "hostname", "sensors",
    ]


class PermissionsSettings(BaseModel):
    fs_read_roots: list[str] = Field(default_factory=lambda: [str(Path.home())])
    # Writes default to a dedicated scratch workspace — NOT Sali's own source tree — so the
    # model can never silently rewrite Sali's code/.git for persistence (red-team crit #3).
    fs_write_roots: list[str] = Field(
        default_factory=lambda: [str(Path.home() / ".local" / "share" / "sali" / "workspace")]
    )
    fs_deny: list[str] = Field(default_factory=_default_fs_deny)
    exec_allowlist: list[str] = Field(default_factory=_default_exec_allowlist)
    exec_cwd: str = Field(default_factory=lambda: str(Path.home() / "Desktop" / "sali"))
    exec_allow_network: bool = False
    jail: bool = True  # use bubblewrap when available


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
