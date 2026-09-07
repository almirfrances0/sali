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
    # Context window (num_ctx). Prompt 6: Sali operates in a SMALL working set (~24K), so the physical
    # KV cache is sized to match — the smaller the num_ctx, the less VRAM/CPU-overflow/latency per turn
    # on this 12 GB card, and the Task State Capsule + retrieval provide the effective long-term memory.
    # The compaction engine folds and continues past it, so this is a comfort ceiling, not a hard wall.
    ctx_default: int = 24_576
    ctx_max: int = 131_072
    request_timeout_s: float = 600.0  # long tasks and large contexts need more time
    # How long Ollama keeps the model resident after a call. Sali is a live resident, not a batch
    # job — evicting the 35B after 5 min idle means the next "hey sali" pays a ~20s cold reload.
    # Keep it warm; "-1" would pin it forever (at the cost of held VRAM).
    keep_alive: str = "24h"  # dedicated box: a 30m idle window forced a ~16GB cold reload (audit)
    # The EMBEDDER gets its own residency, and it is not a copy-paste of the line above.
    #
    # `embed()` passed no keep_alive at all, so it inherited Ollama's 5-minute default while the chat
    # model was pinned for 24h. Measured: with the embedder evicted, the first `memory.retrieve` of a
    # turn took 6,951 ms; the very next one, warm, took 30 ms. That ~7-second cliff landed on exactly
    # the turn Almir comes back to after a break — the one that should feel fastest.
    #
    # It is safe to pin because it is 376 MB and CPU-ONLY (embed_num_gpu = 0): it costs host RAM, of
    # which this box uses 4 of 15 GB, and not one byte of the 923 MiB of free VRAM. Shorter than the
    # chat model's 24h because an embedder that has genuinely gone unused all day is worth releasing.
    embed_keep_alive: str = "8h"
    # ── Power envelope (host survival, 2026-09-01 post-mortem) ────────────────────────────────────
    # sali:latest is a 35B MoE with ~3B active. That "3B active" holds for GENERATION and is
    # dangerously false for PREFILL: a full prompt-processing batch routes across essentially every
    # expert, so prefill touches all 35B of weights. With ~40% of layers on the CPU it streams GBs of
    # expert weights through every core at full tilt WHILE the GPU runs flat out — the highest
    # combined draw this box can produce. Sustained, that tripped the PSU and powered the machine off.
    #
    # These two settings shave the peak of that transient. They cost prefill throughput and buy
    # continued existence, which is the right trade for a resident (see scripts/power_envelope.sh for
    # the hardware half of the same envelope — software alone cannot save a committed prefill).
    num_thread: int = 8      # CPU threads for the offloaded layers (of 16) — never all-core AVX
    # 2048, and the value is measured, not inherited. This machine runs a 22.7 GB model on a 12 GB
    # card: 8,833 MiB of weights sit in VRAM and 13,878 MiB stay CPU-mapped, so every prefill batch
    # streams those host-resident weights across PCIe. Batch size therefore sets how many times that
    # happens: a 10k-token prompt is 20 streams at 512 and 5 at 2048.
    # Measured on a real cold turn, same prompt size, nothing else changed:
    #     512  -> 375 tok/s   (10,466 tokens = 28.0 s)   <- ollama's default
    #    2048  -> 690 tok/s   (10,466 tokens = 15.2 s)   <- 1.84x, +48 MiB VRAM
    #    4096  -> 662 tok/s   (10,540 tokens = 15.9 s)   <- past the knee, slower
    # This is the single largest latency win available on this host and it costs no capability and no
    # context. It is a runner-fingerprint option (see provider/ollama._runner_options), so changing it
    # reloads the model once.
    num_batch: int = 2048


class RuntimeSettings(BaseModel):
    # Generous ceilings, not tight limits: a long multi-step task keeps going (the context is
    # folded and continued when it fills — see the loop), so these are just runaway backstops.
    # Sali must be free to work through complex tasks without hitting artificial walls.
    max_iterations: int = 80
    token_budget_per_run: int = 1_000_000
    resume_interrupted: bool = True  # on startup recovery, actually RE-DRIVE safe/verified runs (§9)
    resume_max_runs: int = 5  # cap how many stale runs one recovery pass will re-drive (a backstop)
    # Prompt 6: Sali's OPERATIONAL context budget — a small, fast working set, NOT the model's real
    # window. The model may support 32K+, but Sali deliberately works in ~24K so KV-cache/VRAM/latency
    # stay low on this hardware; durable state (the Task State Capsule + retrieval) is the real memory.
    # Configurable via SALI_RUNTIME__CONTEXT_LIMIT; the effective limit is min(this, provider window).
    context_limit: int = 24_576
    output_reserve: int = 3_072  # tokens always kept free for the model's reply (§18)
    # Brain-audit Turn 1: on pure conversational turns (iter 0, no primary task, not
    # internal, not a delegation), skip the ~10.6k-token native tools= payload. If the
    # model wants to act it can say so and the next iteration includes tools. Set True to
    # restore the pre-audit behaviour (always send tools) as a kill switch.
    always_send_tools: bool = False


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
    # ~/.config/sali is ALWAYS denied (Final audit §28): it holds Sali's own Fernet vault key + encrypted
    # vault + the host/owner API token — the same agent must never be able to read the key that decrypts its
    # own secrets, or surface its own break-glass credential (defense against prompt-injected exfiltration).
    h = Path.home()
    return [
        str(h / ".ssh"), str(h / ".gnupg"), str(h / ".aws"),
        str(h / ".git-credentials"), str(h / ".netrc"), str(h / "Desktop" / "salix"),
        str(h / ".config" / "sali"),
    ]


def _default_fs_readonly() -> list[str]:
    # Sali may READ its own installation — inspect its code, understand itself (sali3 §15) — but must
    # not write, move, rename, or delete it, so it can never corrupt its own runtime (sali3 §14).
    return [str(Path(__file__).resolve().parents[3])]  # the repo root, e.g. ~/Desktop/sali


def _default_workspace() -> str:
    """Where Sali's work lives. ``SALI_WORKS_ROOT`` overrides it — the ONE knob that moves the whole
    workspace, so a test run cannot bind task workspaces inside Almir's real archive. Production sets
    nothing and gets the original path."""
    import os
    root = os.environ.get("SALI_WORKS_ROOT")
    return root if root else str(Path.home() / "Desktop" / "sali-works")


def _default_skills_root() -> str:
    # Human-editable Markdown skills live at the repo root (Sali's install), so Almir can add/edit a
    # `.md` skill without touching Python. Read-only to Sali itself (it lives under fs_readonly).
    return str(Path(__file__).resolve().parents[3] / "skills")


class PushSettings(BaseModel):
    """APNs push for the iPhone app (§11). Inert until an Apple auth key actually exists.

    The key is a CREDENTIAL and lives in the encrypted vault under `key_ref`
    (`sali secrets set apns.key`); `key_path` is only for a .p8 that already lives elsewhere on
    disk. Neither this file nor Postgres ever holds the key itself (§21/§22).
    """

    enabled: bool = True
    team_id: str = ""                 # Apple Developer Team ID (10 chars)
    key_id: str = ""                  # the .p8 key's own 10-char Key ID
    key_ref: str = "apns.key"         # vault ref holding the .p8 contents
    key_path: str | None = None       # alternative: a readable .p8 on disk
    topic: str = "com.salieno.sali"   # the app's bundle id — APNs calls this the topic


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
    # Where human-editable Markdown skills are discovered (Prompt 5 §6).
    skills_root: str = Field(default_factory=_default_skills_root)
    exec_cwd: str = Field(default_factory=lambda: str(Path.home()))
    exec_allow_network: bool = True  # Sali runs freely, including networked commands (installs)
    jail_learning: bool = True  # an isolated sandbox is available for learning-time experiments


class TemporalSettings(BaseModel):
    """Where the owner actually is, in time.

    This is configuration and not a reading of the host, because on this system those disagree by
    seven hours: Sali's machine is set to America/New_York while Almir works in Africa/Dar_es_Salaam.
    Nothing in the codebase had any notion of a timezone, so "remind me at 9am" would have been
    resolved against the host's zone and fired at 02:00 his time. UTC stays the canonical internal
    timeline; this only decides what a civil-time phrase MEANS and how an instant is shown to him."""

    owner_timezone: str = "UTC"


class ApiSettings(BaseModel):
    """FastAPI / uvicorn network settings and LAN-discovery advertisement.

    Bind default is `0.0.0.0` so a device on the same Wi-Fi/LAN can reach Sali directly (this is
    what enables Bonjour discovery to be useful — the iPhone finds a service that's actually
    reachable). Auth is IP-agnostic: every existing `require_identity`/`require_controller` gate
    still runs on a LAN packet exactly as it does on a Cloudflare-tunnel packet. Set to
    `127.0.0.1` explicitly (via env `SALI_API__BIND_HOST=127.0.0.1` or the TOML config file) to
    force loopback-only for hardened deployments.

    mDNS advertisement is on by default. The iPhone browses for `_sali._tcp.local.`; the TXT
    record carries only public metadata (service tag, stable runtime_id, version, path to the
    unauth `/identity` verifier). Never tokens.
    """

    bind_host: str = "0.0.0.0"
    bind_port: int = 8080
    mdns_enabled: bool = True
    # A friendly display name for the Bonjour instance. Rarely useful to change; distinguishing
    # multiple Salis on the same LAN (test rigs) is what this exists for.
    mdns_service_name: str = "Sali"


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
    push: PushSettings = Field(default_factory=PushSettings)
    temporal: TemporalSettings = Field(default_factory=TemporalSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)

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
