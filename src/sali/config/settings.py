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
    # How long Ollama keeps the model resident after a call. Sali is a live resident, not a batch
    # job — evicting the 35B after 5 min idle means the next "hey sali" pays a ~20s cold reload.
    # Keep it warm; "-1" would pin it forever (at the cost of held VRAM).
    keep_alive: str = "30m"


class RuntimeSettings(BaseModel):
    # Generous ceilings, not tight limits: a long multi-step task keeps going (the context is
    # folded and continued when it fills — see the loop), so these are just runaway backstops.
    max_iterations: int = 40
    token_budget_per_run: int = 400_000


class SshSettings(BaseModel):
    # Remote work goes over the system ssh, reusing Almir's ~/.ssh (keys, config aliases, agent) —
    # no passwords are ever stored. 'fake' selects the in-memory runner for tests (no socket).
    backend: Literal["ssh", "fake"] = "ssh"
    connect_timeout: int = 10  # seconds — ssh -o ConnectTimeout, fails fast, never hangs
    command_timeout: float = 120.0  # seconds — cap on a single remote command (a VPS analysis runs long)


class MailAccountConfig(BaseModel):
    # NON-secret inventory only — the app-password lives in the SecretStore under `secret_ref`.
    address: str
    imap_host: str
    smtp_host: str
    imap_port: int = 993
    smtp_port: int = 465
    secret_ref: str = "mail.password"
    # Reading is always IMAP. Sending is 'smtp' by default, or 'gmail_api' (HTTPS) for networks that
    # block outbound SMTP — its OAuth creds live in the SecretStore under these refs.
    send_backend: Literal["smtp", "gmail_api"] = "smtp"
    gmail_client_id_ref: str = "gmail.client_id"
    gmail_client_secret_ref: str = "gmail.client_secret"
    gmail_refresh_token_ref: str = "gmail.refresh_token"


class CalAccountConfig(BaseModel):
    url: str  # CalDAV collection URL
    username: str
    secret_ref: str = "caldav.password"


class CommsSettings(BaseModel):
    mail: MailAccountConfig | None = None
    calendar: CalAccountConfig | None = None


class BrowserSettings(BaseModel):
    # Sali's own headless Firefox via Playwright. 'fake' selects the in-memory browser for tests.
    backend: Literal["playwright", "fake"] = "playwright"
    headless: bool = True
    firefox_profile: str | None = None  # profile dir to import login cookies from; None = auto-detect
    import_cookies: bool = True  # seed Sali's browser with Almir's Firefox session cookies
    mem_floor_mb: int = 1500  # don't launch below this much free RAM (this box is 15GB)


def _default_watch_roots() -> list[str]:
    return [str(Path.home() / "Desktop")]  # where Almir works; noise dirs are filtered deterministically


class PerceptionSettings(BaseModel):
    # Desktop perception (sali3 Phase 4): the focused app/window (X11) + accessibility tree (AT-SPI).
    # 'fake' selects the in-memory perception for tests.
    backend: Literal["desktop", "fake"] = "desktop"
    max_tree_nodes: int = 200  # cap the accessibility walk so a huge UI can't flood the context
    max_tree_depth: int = 12
    # AT-SPI lives in the SYSTEM python (PyGObject); Sali's venv (3.14) has none, so the probe runs here.
    system_python: str = "/usr/bin/python3"
    # Continuous event engine (sali3 Phase 5): watch the filesystem + active window, score, aggregate.
    watch_enabled: bool = True  # run the event engine inside `sali daemon`
    watch_roots: list[str] = Field(default_factory=_default_watch_roots)
    window_poll_s: float = Field(default=3.0, gt=0)  # how often to sample the focused window
    aggregate_window_s: float = Field(default=2.0, gt=0)  # quiet period before a burst flushes as one
    observation_buffer: int = 200  # bounded ring of recent observations


def _default_fs_deny() -> list[str]:
    # This machine is Sali's home — it's open to it. Only credentials and the separate
    # `salix` project stay protected by default (a light guard the owner can remove).
    h = Path.home()
    return [
        str(h / ".ssh"), str(h / ".gnupg"), str(h / ".aws"),
        str(h / ".git-credentials"), str(h / ".netrc"), str(h / "Desktop" / "salix"),
    ]


def _default_fs_readonly() -> list[str]:
    # Sali may READ its own installation — inspect its code, understand itself (sali3 §15) — but must
    # not write, move, rename, or delete it, so it can never corrupt its own runtime (sali3 §14).
    return [str(Path(__file__).resolve().parents[3])]  # the repo root, e.g. ~/Desktop/sali


def _default_workspace() -> str:
    return str(Path.home() / "Desktop" / "sali-works")


class PermissionsSettings(BaseModel):
    # The whole machine is open to Sali: it can read and create files anywhere. The real guards
    # are the OS's own permissions (it runs as Almir, not root), the small fs_deny set below, and
    # the loop's confirm-before-destructive judgment — not a hardcoded map of allowed folders.
    fs_read_roots: list[str] = Field(default_factory=lambda: ["/"])
    fs_write_roots: list[str] = Field(default_factory=lambda: ["/"])
    fs_deny: list[str] = Field(default_factory=_default_fs_deny)
    # Readable but NEVER writable — Sali's own installation (sali3 §14-17): self-inspection without
    # self-corruption. A write/delete under one of these is refused before it even reaches the tool.
    fs_readonly: list[str] = Field(default_factory=_default_fs_readonly)
    # Sali's own working area (sali3 §13): full, unconfirmed CRUD lives here.
    workspace: str = Field(default_factory=_default_workspace)
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
    ssh: SshSettings = Field(default_factory=SshSettings)
    comms: CommsSettings = Field(default_factory=CommsSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    perception: PerceptionSettings = Field(default_factory=PerceptionSettings)

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
