"""The `sali` command-line interface — one Sali, many windows.

Every command here is exactly one of three things, and it says which:

* **THE MIND** — ``sali daemon`` (and ``sali serve``, its API-only sibling). Takes machine-wide
  ownership via :mod:`sali.core.mind` and owns the one Kernel, AgentRuntime, AgentLoop, coordinator
  and inference path. A second one fails safely instead of starting.
* **A WINDOW** — ``sali agent``. If Sali is already alive it *attaches* to him over the local API
  discovered from the mind lock and loads no model at all. Only when nobody is alive does it become
  the mind itself, which keeps the invariant true rather than merely convenient.
* **A UTILITY** — migrations, backups, inspection, enrolment codes. Datastore work; no runtime, and
  no cognition while the mind lives (:mod:`sali.provider.authority` enforces that at the model
  boundary, and ``_refuse_if_mind_alive`` says so early, in words, rather than mid-run).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from sali import __version__
from sali.config.settings import Settings, load_settings
from sali.context.engine import IDENTITY
from sali.core.mind import ProcessRole, describe_holder, live_holder, set_current_role
from sali.obs.log import configure_logging
from sali.runtime.session import background_session_id

app = typer.Typer(add_completion=False, help="Sali — a local-first personal AI agent.")
console = Console()


# ── One Sali: the CLI's side of the invariant ─────────────────────────────────────────────────────
def _refuse_if_mind_alive(what: str, *, suggest: str = "sali agent") -> None:
    """Stop a command that would duplicate a faculty of the living Sali.

    These commands (`learn`, `scheduler`, `observe`, `chat`, …) each drive the model or run a loop
    that the daemon already runs as part of Sali's life. Running one alongside him is not a second
    opinion, it is a second mind. The inference authority would refuse them anyway at the model
    boundary; refusing here turns an obscure mid-run ProviderError into a sentence.
    """
    holder = live_holder()
    if holder is None:
        return
    console.print(f"[yellow]Sali is already alive — `{what}` belongs to him, not beside him.[/]")
    console.print(f"[dim]{describe_holder(holder)}[/]")
    console.print(f"[dim]Talk to him with [bold]{suggest}[/], or stop him first: "
                  "sudo systemctl stop sali[/]")
    raise typer.Exit(1)


def _as_utility() -> None:
    """Mark this process a utility: datastore work, never a mind."""
    set_current_role(ProcessRole.UTILITY)

secrets_cli = typer.Typer(help="Manage Sali's secrets — never stored in the database.")
app.add_typer(secrets_cli, name="secrets")

memory_cli = typer.Typer(help="Inspect Sali's memory.")
app.add_typer(memory_cli, name="memory")

tools_cli = typer.Typer(help="Inspect the tools Sali has discovered on this machine.")
app.add_typer(tools_cli, name="tools")


@memory_cli.command("eval")
def memory_eval() -> None:
    """Benchmark the memory system (§67): seed a controlled scenario, score retrieval / resolution /
    temporal / provenance / contradiction / scope accuracy. Runs rolled-back — never touches real data."""
    settings = load_settings()
    configure_logging("ERROR")
    _refuse_if_mind_alive("memory eval", suggest="sali agent  # ask him to check his own recall")
    asyncio.run(_memory_eval(settings))


async def _memory_eval(settings: Settings) -> None:
    from sali.kernel import Kernel
    from sali.provider.registry import build_provider
    from sali.retrieval.benchmark import run_benchmark

    kernel = Kernel.create(settings)
    try:
        card = await run_benchmark(await kernel.pool(), build_provider(settings))
    finally:
        await kernel.close()
    for name, dimension, ok in card.results:
        mark = "[green]✓[/]" if ok else "[red]✗[/]"
        console.print(f"  {mark} [dim]{dimension:16s}[/] {name}")
    console.print()
    for dim, (passed, total) in sorted(card.by_dimension.items()):
        colour = "green" if passed == total else "yellow" if passed else "red"
        console.print(f"[bold]{dim:16s}[/] [{colour}]{passed}/{total}[/]")
    pct = round(100 * card.passed / card.total) if card.total else 0
    console.print(f"\n[bold]Memory score:[/] {card.passed}/{card.total} ({pct}%)")


@memory_cli.command("status")
def memory_status() -> None:
    """Show the health of Sali's memory — counts by layer, graph size, and soft spots (§49)."""
    settings = load_settings()
    configure_logging("ERROR")
    asyncio.run(_memory_status(settings))


async def _memory_status(settings: Settings) -> None:
    from sali.kernel import Kernel
    from sali.memory import diagnostics

    kernel = Kernel.create(settings)
    try:
        h = await diagnostics.health(await kernel.pool())
    finally:
        await kernel.close()
    m, g, c = h["memory"], h["graph"], h["contradictions"]
    console.print(f"[bold]Memory[/] — {m['current']} current, {m['superseded']} superseded, "
                  f"{m.get('scoped', 0)} project-scoped")
    for layer, n in m["by_layer"].items():
        console.print(f"  {layer:12s} {n}")
    console.print(f"[bold]Graph[/] — {g['nodes']} nodes, {g['edges']} edges "
                  f"({g['historical_edges']} historical), {g['orphan_nodes']} orphans, "
                  f"{g.get('corroborated_facts', 0)} multi-source")
    console.print(f"[bold]Contradictions[/] — {c['total']} total, {c['open']} open, "
                  f"{c.get('verified', 0)} verified, {c.get('by_priority', 0)} by-priority, "
                  f"{c.get('possible', 0)} possible (free-text)  [bold]Events[/] — {h['events']}")
    soft = []
    if m["unverified"]:
        soft.append(f"{m['unverified']} unverified")
    if m["low_confidence"]:
        soft.append(f"{m['low_confidence']} low-confidence")
    if m["embed_backlog"]:
        soft.append(f"{m['embed_backlog']} awaiting embedding")
    if g["orphan_nodes"]:
        soft.append(f"{g['orphan_nodes']} orphan nodes")
    console.print("[yellow]Soft spots:[/] " + (", ".join(soft) if soft else "none — memory is healthy"))


@tools_cli.command("discover")
def tools_discover() -> None:
    """Scan the machine now: discover tools, map capabilities, classify authority (§26)."""
    settings = load_settings()
    configure_logging("ERROR")
    asyncio.run(_tools_discover(settings))


async def _tools_discover(settings: Settings) -> None:
    from sali.db.pool import create_pool
    from sali.twin import tool_intel

    pool = await create_pool(settings)
    try:
        with console.status("scanning PATH, resolving packages, classifying…"):
            res = await tool_intel.run_pass(pool)
    finally:
        await pool.close()
    if res.skipped:
        console.print("[yellow]another discovery pass is already running — skipped.[/]")
        return
    console.print(f"[green]{res.discovered} tools[/] known "
                  f"([green]+{res.added}[/] / [yellow]-{res.removed}[/]), "
                  f"{res.capability_edges} capability links, {res.authority_written} (re)classified.")


@tools_cli.command("list")
def tools_list(
    capability: str = typer.Option(None, "--capability", "-c", help="only tools providing this capability"),
    authority: str = typer.Option(None, "--authority", "-a", help="normal | elevated | system_critical"),
    limit: int = typer.Option(60, "--limit", "-n"),
) -> None:
    """List discovered tools, optionally filtered by capability or authority tier."""
    settings = load_settings()
    configure_logging("ERROR")
    asyncio.run(_tools_list(settings, capability, authority, limit))


async def _tools_list(settings: Settings, capability: str | None, authority: str | None,
                      limit: int) -> None:
    from sali.db.pool import create_pool
    from sali.twin import tool_report

    pool = await create_pool(settings)
    try:
        async with pool.acquire() as conn:
            rows = await tool_report.list_tools(
                conn, capability=capability, authority=authority, limit=limit)
    finally:
        await pool.close()
    if not rows:
        console.print("[dim]no matching tools (try `sali tools discover` first).[/]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("tool")
    table.add_column("authority")
    table.add_column("capabilities")
    table.add_column("package", style="dim")
    _auth_style = {"system_critical": "red", "elevated": "yellow", "normal": "green"}
    for r in rows:
        auth = r["authority"] or "—"
        caps = ", ".join(r["capabilities"]) if r["capabilities"] else "[dim]—[/]"
        table.add_row(r["name"], f"[{_auth_style.get(auth, 'white')}]{auth}[/]", caps,
                      r["package"] or "")
    console.print(table)
    console.print(f"[dim]{len(rows)} tool(s).[/]")


@tools_cli.command("inspect")
def tools_inspect(name: str) -> None:
    """Everything Sali knows about one tool: path, package, capabilities, authority, and experience."""
    settings = load_settings()
    configure_logging("ERROR")
    asyncio.run(_tools_inspect(settings, name))


async def _tools_inspect(settings: Settings, name: str) -> None:
    from sali.db.pool import create_pool
    from sali.twin import tool_report

    pool = await create_pool(settings)
    try:
        async with pool.acquire() as conn:
            info = await tool_report.inspect_tool(conn, name)
    finally:
        await pool.close()
    if info is None:
        console.print(f"[yellow]'{name}' is not in Sali's tool inventory.[/] "
                      "It may not be installed, or run `sali tools discover`.")
        return
    console.print(f"[bold]{info['name']}[/]" + (f"  [dim]{info['path']}[/]" if info["path"] else ""))
    if info["package"]:
        console.print(f"  package: {info['package']}"
                      + (f" · version {info['version']}" if info["version"] else ""))
    auth = info["authority"] or "unclassified"
    console.print(f"  authority: [bold]{auth}[/]"
                  + (f" — [dim]{info['authority_rationale']}[/]" if info["authority_rationale"] else ""))
    console.print("  capabilities: " + (", ".join(info["capabilities"]) if info["capabilities"]
                                        else "[dim]none known[/]"))
    if info["experience_summary"]:
        console.print(f"  experience: {info['experience_summary']}")
        exp = info["experience"] or {}
        if exp.get("failure_modes"):
            console.print("    common failures: " + "; ".join(exp["failure_modes"]))
    else:
        console.print("  experience: [dim]not used yet[/]")


@tools_cli.command("coverage")
def tools_coverage() -> None:
    """Tool-knowledge coverage: discovered vs mapped vs classified vs actually-used (§25)."""
    settings = load_settings()
    configure_logging("ERROR")
    asyncio.run(_tools_coverage(settings))


async def _tools_coverage(settings: Settings) -> None:
    from sali.db.pool import create_pool
    from sali.tools.registry import default_registry
    from sali.twin import tool_report

    pool = await create_pool(settings)
    try:
        async with pool.acquire() as conn:
            cov = await tool_report.coverage(conn)
    finally:
        await pool.close()
    registered = len(default_registry())
    console.print(f"[bold]Discovered tools[/] — {cov['discovered']} installed")
    console.print(f"  with known capability : {cov['with_capability']} "
                  f"([dim]{cov['capabilities']} capabilities in the vocabulary[/])")
    console.print(f"  classified for authority: {cov['classified']}  "
                  + "  ".join(f"{k}={v}" for k, v in sorted(cov["by_authority"].items())))
    console.print(f"  actually used by Sali : {cov['used']}  "
                  f"([yellow]{cov['never_used']} never used[/])")
    if cov["top_used"]:
        console.print("  most used: " + ", ".join(f"{b} ({n})" for b, n in cov["top_used"]))
    console.print(f"[dim]Built-in agent tools registered: {registered}[/]")


@tools_cli.command("classify")
def tools_classify(name: str) -> None:
    """Ask the local model to judge a tool's danger (§34) — it can only RAISE the deterministic tier,
    never lower it. Records an escalation; the safe default always stands."""
    settings = load_settings()
    configure_logging("ERROR")
    _refuse_if_mind_alive("tools classify")
    asyncio.run(_tools_classify(settings, name))


async def _tools_classify(settings: Settings, name: str) -> None:
    from sali.core.enums import MemorySource
    from sali.db.pool import create_pool
    from sali.provider.registry import build_provider
    from sali.twin.authority import classify_authority, current_authority, set_authority
    from sali.twin.interpret import interpret_authority

    det_tier, _ = classify_authority(name)
    with console.status(f"asking the model about {name}…"):
        tier, rationale = await interpret_authority(build_provider(settings), name)
    console.print(f"[bold]{name}[/] — deterministic: [bold]{det_tier.value}[/]; "
                  f"final: [bold]{tier.value}[/]")
    if tier is not det_tier:
        console.print(f"  [yellow]{rationale}[/]")
    pool = await create_pool(settings)
    try:
        async with pool.acquire() as conn:
            was = await current_authority(conn, name)
            if was is None:
                console.print("[dim](not in the inventory — run `sali tools discover`; nothing recorded)[/]")
                return
            recorded = await set_authority(conn, name, tier, rationale=rationale,
                                           classifier="llm_interpret", source=MemorySource.EXTERNAL_SOURCE)
    finally:
        await pool.close()
    if recorded and tier is not was:
        console.print(f"  [green]recorded[/] {was.value} → {tier.value}")
    else:
        console.print(f"  [dim]unchanged ({tier.value})[/]")


@secrets_cli.command("set")
def secrets_set(
    ref: str,
    from_file: str = typer.Option(
        "", "--from-file",
        help="Read the value from this file instead of prompting (for multi-line keys like an APNs .p8)."),
) -> None:
    """Store a secret (e.g. 'mail.personal.password'), ENCRYPTED at rest in ~/.config/sali/vault.json.

    The hidden prompt reads ONE line, which a PEM private key is not — `--from-file` is how a
    multi-line credential gets in without ever being echoed or landing in shell history."""
    from sali.config.secrets import SecretStore
    from sali.config.vault import VaultError

    if from_file:
        source = Path(from_file).expanduser()
        if not source.is_file():
            raise typer.BadParameter(f"no readable file at {source}")
        value = source.read_text()
        if not value.strip():
            raise typer.BadParameter(f"{source} is empty — nothing to store")
    else:
        value = typer.prompt(f"value for {ref}", hide_input=True)
    try:
        SecretStore().set(ref, value)
    except VaultError as exc:
        raise typer.BadParameter(f"vault refused the write (no secret was lost): {exc}") from exc
    console.print(f"[green]stored[/] {ref} [dim](encrypted at rest; value never shown, never in the DB)[/]")


@secrets_cli.command("list")
def secrets_list() -> None:
    """List the secret NAMES that are configured (never the values)."""
    from sali.config.secrets import SecretStore
    from sali.config.vault import VaultError

    try:
        refs = SecretStore().refs()
    except VaultError as exc:
        raise typer.BadParameter(f"vault is unreadable: {exc}") from exc
    if not refs:
        console.print("[dim]No secrets set.[/]")
        return
    for ref in refs:
        console.print(f"• {ref}")


@secrets_cli.command("rm")
def secrets_rm(ref: str) -> None:
    """Delete a secret from the encrypted vault."""
    from sali.config.secrets import SecretStore
    from sali.config.vault import VaultError

    try:
        removed = SecretStore().delete(ref)
    except VaultError as exc:
        raise typer.BadParameter(f"vault refused the delete (no secret was lost): {exc}") from exc
    console.print(f"[green]removed[/] {ref}" if removed else f"[yellow]not in the vault:[/] {ref}")


@app.command("sudo-pass")
def sudo_pass() -> None:
    """Store your sudo password (encrypted in the vault) so Sali can run privileged commands. The
    password never enters Sali's reasoning, prompts, or logs — sudo pulls it via SUDO_ASKPASS (§28)."""
    from sali.config.secrets import SecretStore
    from sali.config.vault import VaultError
    from sali.tools.privilege import SUDO_REF

    value = typer.prompt("your sudo password", hide_input=True)
    try:
        SecretStore().set(SUDO_REF, value)
    except VaultError as exc:
        raise typer.BadParameter(f"vault refused the write (no secret was lost): {exc}") from exc
    console.print("[green]stored[/] sudo.password "
                  "[dim](encrypted; used only via SUDO_ASKPASS, never shown or logged)[/]")


@app.command("sudo-askpass", hidden=True)
def sudo_askpass() -> None:
    """Internal SUDO_ASKPASS helper — prints the stored sudo password to stdout for sudo. Not for
    interactive use; it only exposes what the same user could already read from the vault."""
    import sys

    from sali.tools.privilege import read_sudo_password

    password = read_sudo_password()
    if password:
        sys.stdout.write(password)


@app.command("ssh-pass")
def ssh_pass(host: str) -> None:
    """Store a password for a VPS/host (user@host or a ~/.ssh alias) in the encrypted vault, so Sali
    can ssh into a key-less box. You can also just tell Sali the password once and it saves it."""
    from sali.config.secrets import SecretStore
    from sali.config.vault import VaultError
    from sali.tools.remote import password_ref

    ref = password_ref(host)
    value = typer.prompt(f"ssh password for {host}", hide_input=True)
    try:
        SecretStore().set(ref, value)
    except VaultError as exc:
        raise typer.BadParameter(f"vault refused the write (no secret was lost): {exc}") from exc
    console.print(f"[green]stored[/] {ref} [dim](encrypted; Sali will use it for {host})[/]")


@secrets_cli.command("migrate")
def secrets_migrate() -> None:
    """Move legacy plaintext secrets (~/.config/sali/secrets.toml) into the encrypted vault."""
    from sali.config.secrets import SecretStore
    from sali.config.vault import VaultError

    try:
        migrated = SecretStore().migrate_legacy()
    except VaultError as exc:
        raise typer.BadParameter(f"vault refused the migration (no secret was lost): {exc}") from exc
    if not migrated:
        console.print("[dim]Nothing to migrate (no legacy plaintext secrets, or all already in the vault).[/]")
        return
    for ref in migrated:
        console.print(f"[green]encrypted[/] {ref}")
    console.print("[dim]Verify with `sali secrets list`, then delete ~/.config/sali/secrets.toml.[/]")


@app.command()
def version() -> None:
    """Print Sali's version."""
    console.print(f"Sali {__version__}")


@app.command()
def init() -> None:
    """Apply database migrations to the configured database."""
    settings = load_settings()
    configure_logging(settings.log_level)
    applied = asyncio.run(_init(settings))
    if applied:
        console.print(f"[green]Applied migrations:[/] {', '.join(applied)}")
    else:
        console.print("[green]Database already up to date.[/]")


async def _init(settings: Settings) -> list[str]:
    from sali.db.bootstrap import init_database

    applied = await init_database(settings)
    await _seed_core(settings)  # idempotent: Sali's identity persists independent of the model
    await _seed_twin(settings)  # best-effort: know the machine right after setup
    return applied


async def _seed_twin(settings: Settings) -> None:
    """Build the desktop twin once at init so Sali starts out knowing its machine. Best-effort —
    a failure here never blocks init (the twin can always be (re)built with `sali twin --refresh`)."""
    from sali.db.pool import create_pool

    pool = await create_pool(settings)
    try:
        result = await _twin_service(pool, settings).refresh(
            exclude_projects=tuple(settings.permissions.fs_deny)
        )
        console.print(f"[green]Built the desktop twin:[/] {result.entities} things observed.")
    except Exception as exc:  # noqa: BLE001 - observation is optional at init time
        console.print(f"[yellow]Twin not built ({exc}); run `sali twin --refresh` later.[/]")
    finally:
        await pool.close()


async def _seed_core(settings: Settings) -> None:
    """Seed Sali's identity + root graph (idempotent). Identity is owned by Sali, not the
    model (spec §27), so it lives in the datastore, not a prompt constant."""
    from sali.core.enums import MemoryLayer, MemorySource
    from sali.db.pool import connect
    from sali.graph import writer as graph_writer
    from sali.memory import writer as memory_writer

    conn = await connect(settings)
    try:
        # The single canonical person (§19). Self-reference forms resolve here so "what do I use?"
        # finds Almir's graph, never a stray node whose path happens to contain "almir" (§18).
        almir = await graph_writer.ensure_node(
            conn, node_type="person", name="Almir", canonical_key="person:almir",
            source=MemorySource.USER_EXPLICIT,
            props={"aliases": ["me", "my", "i", "myself", "mine", "owner", "the user", "almir"]},
        )
        sali = await graph_writer.ensure_node(
            conn, node_type="agent", name="Sali", canonical_key="agent:sali",
            source=MemorySource.USER_EXPLICIT,
        )
        # Structured identity facts on the agent node (§2/§3) — presentation, pronouns, role, owner, and
        # how Sali addresses Almir. Merged with `||` so it never clobbers aliases the linker adds; the
        # self-view reads these instead of a hardcoded literal, so identity answers come from state.
        # Pass the DICT, not json.dumps(dict): this connection has an asyncpg jsonb codec whose encoder is
        # json.dumps, so a pre-dumped STRING gets encoded AGAIN into a jsonb string scalar and `||` then
        # makes an ARRAY, corrupting the props (measured). A dict encodes cleanly to a jsonb object.
        await conn.execute(
            "UPDATE graph_node SET props = coalesce(props,'{}'::jsonb) || $1::jsonb "
            "WHERE id=$2 AND valid_until IS NULL",
            {"presentation": "male", "pronouns": "he/him",
             "role": "Almir personal AI companion and assistant",
             "preferred_address": "Almir", "owner": "Almir"},
            sali.id,
        )
        await graph_writer.relate(
            conn, src_id=almir.id, dst_id=sali.id, rel_type="uses",
            source=MemorySource.USER_EXPLICIT,
        )
        # The reciprocal: Sali SERVES Almir (§ Phase 5). This gives Almir's relationships — his people,
        # his projects — a real person:almir node + edge to attach to, instead of "Almir" living only as
        # a string prop on the agent node. No behaviour changes until those edges exist; it's an enabler.
        await graph_writer.relate(
            conn, src_id=sali.id, dst_id=almir.id, rel_type="serves",
            source=MemorySource.USER_EXPLICIT,
        )
        await conn.execute(
            "UPDATE graph_node SET props = coalesce(props,'{}'::jsonb) || $1::jsonb "
            "WHERE id=$2 AND valid_until IS NULL",
            {"pronouns": "he/him", "role": "owner"}, almir.id)
        await memory_writer.remember(
            conn, layer=MemoryLayer.IDENTITY,
            content=(
                "I am Sali, Almir's local AI. I am not the underlying model; my memory, graph, "
                "tools, learned procedures, and identity persist even if the reasoning model changes."
            ),
            source=MemorySource.USER_EXPLICIT, importance=0.95,
        )
    finally:
        await conn.close()


@app.command()
def backup(
    dest: str = typer.Option("", help="Where to write the backup (default ~/.local/share/sali/backups)."),
    keep: int = typer.Option(7, help="How many backups to keep (older ones are rotated out)."),
) -> None:
    """Back up the datastore + config/vault locally, and rotate old backups (§52). The datastore holds
    no raw secrets; the vault stays encrypted. Secrets never leave the machine."""
    from pathlib import Path

    from sali.db import backup as db_backup

    settings = load_settings()
    where = Path(dest).expanduser() if dest else db_backup.default_dir()
    try:
        result = db_backup.run_backup(settings, where, keep=keep)
    except FileNotFoundError:
        raise typer.BadParameter("pg_dump not found — install postgresql-client") from None
    except Exception as exc:  # noqa: BLE001 - a failed backup must be loud, with the reason
        raise typer.BadParameter(f"backup failed: {str(exc)[:300]}") from exc
    mb = result["dump_bytes"] / 1_000_000
    console.print(f"[green]backed up[/] datastore → {result['dump']} [dim]({mb:.1f} MB)[/]")
    console.print(f"[dim]config/vault → {result['dir']}/config-… ({', '.join(result['config'])})[/]")
    if result["rotated"]:
        console.print(f"[dim]rotated out {result['rotated']} old backup(s)[/]")
    if result["has_secret_key"]:
        console.print("[yellow]note:[/] this backup contains the vault key — keep the directory as "
                      "protected as ~/.config/sali (it can decrypt your secrets).")


@app.command()
def doctor() -> None:
    """Check Postgres connectivity, migration state, and Ollama."""
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_doctor(settings))


async def _doctor(settings: Settings) -> None:
    from sali.db.migrations.runner import current_version
    from sali.db.pool import connect
    from sali.provider.registry import build_provider

    table = Table(title="sali doctor", show_lines=False)
    table.add_column("check", style="bold")
    table.add_column("status")
    table.add_column("detail", overflow="fold")

    try:
        conn = await connect(settings)
        try:
            server = await conn.fetchval("SELECT version()")
            mig = await current_version(conn)
        finally:
            await conn.close()
        head = str(server).split(",", 1)[0]
        table.add_row("postgresql", "[green]ok[/]", f"{head}; migration={mig or 'none'}")
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        table.add_row("postgresql", "[red]FAIL[/]", str(exc))

    try:
        provider = build_provider(settings)
        ok = await provider.health()
        status = "[green]ok[/]" if ok else "[red]FAIL[/]"
        table.add_row("ollama", status, f"{settings.model.host} · model={settings.model.chat_model}")
    except Exception as exc:  # noqa: BLE001
        table.add_row("ollama", "[red]FAIL[/]", str(exc))

    holder = live_holder()
    table.add_row("one mind", "[green]ok[/]" if holder is None else "[cyan]alive[/]",
                  describe_holder(holder))

    console.print(table)
    if holder is not None:
        console.print("[dim]Run [bold]sali status[/] for the full single-mind picture "
                      "(ownership, model residency, cognition slot).[/]")


@app.command()
def status() -> None:
    """Prove the invariant: who is THE Sali, what holds ownership, and what the GPU is really running.

    Everything here is *observed*, not asserted — the flock, PostgreSQL's own lock table, and
    `ollama ps` — so it can contradict the code if the code is wrong. That is the point.
    """
    settings = load_settings()
    configure_logging("ERROR")
    _as_utility()
    asyncio.run(_status(settings))


async def _status(settings: Settings) -> None:
    from sali.core.mind import default_lock_path
    from sali.db.mind_lock import current_db_mind
    from sali.db.pool import create_pool
    from sali.provider import authority
    from sali.provider.registry import build_provider

    t = Table(title="Sali — one machine, one mind", show_lines=False)
    t.add_column("what", style="bold")
    t.add_column("state")
    t.add_column("detail", overflow="fold")

    # 1. Machine-wide ownership (the flock). The authority on "is he alive".
    holder = live_holder()
    t.add_row("mind (file lock)", "[cyan]held[/]" if holder else "[dim]free[/]",
              f"{default_lock_path()} · {describe_holder(holder)}")

    # 2. The datastore's independent opinion, read from pg_locks — not from any process's self-report.
    try:
        pool = await create_pool(settings)
    except Exception as exc:  # noqa: BLE001 - status must work with the DB down
        pool = None
        t.add_row("mind (datastore)", "[yellow]unknown[/]", f"postgres unreachable: {str(exc)[:80]}")
    if pool is not None:
        db_mind = await current_db_mind(pool)
        t.add_row("mind (pg advisory)", "[cyan]held[/]" if db_mind else "[dim]free[/]",
                  (f"backend pid {db_mind['pid']} since "
                   f"{db_mind['backend_start']:%H:%M:%S}") if db_mind else "no holder")

        # 3. The foreground turn lease — a different thing: one TURN at a time, not one MIND.
        from sali.runtime.lease import ExecutionLease
        cur = await ExecutionLease(pool).get_current()
        t.add_row("foreground turn", "[cyan]busy[/]" if cur else "[green]idle[/]",
                  (f"{cur['owner_id']} · origin={cur['origin']}") if cur else "no turn running")
        await pool.close()

    # 4. What ollama is ACTUALLY holding — asked of the server, not inferred from Sali's own state.
    #    A 35B MoE at ~16GB already fills ~91% of a 12GB card; a second copy is a host emergency.
    from sali.provider.residency import server_envelope, survey
    provider = build_provider(settings)
    res = await survey(getattr(provider, "_client", None), settings.model.chat_model)
    if getattr(provider, "_client", None) is None:
        t.add_row("model residency", "[dim]n/a[/]", "no ollama client (fake provider)")
    else:
        state = "[green]ok[/]" if res.ok else "[red]DUPLICATE[/]"
        detail = (f"{res.chat_runners}× {settings.model.chat_model}"
                  f" · {res.vram_gb:.1f} GB VRAM across {len(res.runners)} runner(s)")
        if res.reason:
            detail += f" · {res.reason}"
        if not res.runners:
            detail = "nothing loaded"
        split = [r for r in res.runners if r.offloaded]
        if split:
            detail += " · SPLIT GPU/CPU (prefill drives every core)"
        t.add_row("model residency", state, detail)

    # 5. Ollama's own limits, read from systemd — a drop-in lost to a package upgrade is a hazard
    #    Sali's locks cannot see, because the concurrency would happen inside the server.
    srv = await server_envelope()
    t.add_row("ollama limits", "[green]ok[/]" if srv["ok"] else "[yellow]hazard[/]",
              "; ".join(srv["problems"]) if srv["problems"]
              else f"NUM_PARALLEL={srv['env'].get('OLLAMA_NUM_PARALLEL')} "
                   f"MAX_LOADED_MODELS={srv['env'].get('OLLAMA_MAX_LOADED_MODELS')}")

    # 6. The power envelope. This is the one that ended in a power cut when it was missing.
    from sali.runtime.envelope import read_envelope
    env = read_envelope()
    t.add_row("power envelope", "[green]ok[/]" if env.ok else "[red]UNSAFE[/]",
              env.summary + (" · " + "; ".join(env.problems) if env.problems
                             else f" (chip rated {env.cpu_rated_w}W, card max {env.gpu_max_w:.0f}W)"))

    # 7. What THIS process may do — the inference authority's own answer.
    exp = authority.explain()
    t.add_row("this process", str(exp["process_role"]),
              f"cognition allowed here: {exp['cognition_allowed_here']} · "
              f"embeddings: {exp['embeddings_allowed_here']}")

    console.print(t)
    if holder is None:
        console.print("[dim]No Sali is running. [bold]sudo systemctl start sali[/] gives him a "
                      "permanent life; [bold]sali agent[/] alone makes the terminal the one Sali.[/]")


@app.command()
def agent(
    message: str | None = typer.Argument(None, help="A one-shot question; omit for a REPL."),
) -> None:
    """Talk to Sali.

    A WINDOW, not a Sali. If he is already alive (the systemd `sali` service, or another terminal),
    this attaches to him over his local API and loads no model. Only if nobody is alive does this
    terminal become the one Sali for as long as it runs.
    """
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_agent(settings, message))


@app.command()
def serve(
    port: int = typer.Option(8080, help="Port to listen on."),
    host: str = typer.Option("127.0.0.1", help="Host to bind to (loopback; the tunnel is the ingress)."),
) -> None:
    """Run Sali as an API-only mind (no background life). Normally you want `sali daemon` instead,
    which is the same one mind WITH his faculties and the same API.

    Like every authoritative entry point this takes machine-wide ownership first, so it can never
    become a second Sali beside the daemon — whatever port you give it.
    """
    from sali.api.app import create_app
    from sali.core.mind import SecondMindError
    from sali.kernel import Kernel

    settings = load_settings()
    configure_logging("WARNING")

    async def _run() -> None:
        import uvicorn

        from sali.security.confirm import AutoAllowConfirmer
        try:
            kernel = await Kernel.become_mind(settings, api_host=host, api_port=port)
        except SecondMindError as exc:
            console.print(f"[yellow]{exc}[/]")
            raise typer.Exit(1) from None
        try:
            runtime = await kernel.runtime(confirmer=AutoAllowConfirmer(), ws_broadcaster=_ws_manager())
            api = _api_app(kernel, runtime, await kernel.pool(), create_app)
            kernel.publish_endpoint(host, port)
            console.print(f"[dim]Sali is up (API only) on {host}:{port} — one mind, one model.[/]")
            await uvicorn.Server(uvicorn.Config(
                api, host=host, port=port, log_level="warning")).serve()
        finally:
            await kernel.close()

    asyncio.run(_run())


def _ws_manager() -> Any:
    from sali.api.ws import manager
    return manager


def _api_app(kernel: Any, runtime: Any, pool: Any, create_app: Any) -> Any:
    """The API wired as a WINDOW onto an existing mind: the runtime is injected, never built.

    ``create_app(kernel=None)`` plus an injected ``app.state.runtime`` is the contract that keeps the
    API from ever constructing a second AgentLoop — see ``sali.api.app.create_app``.
    """
    api = create_app(kernel=None)
    api.state.kernel = kernel
    api.state.pool = pool
    api.state.runtime = runtime
    return api


@app.command("enroll-code")
def enroll_code(
    role: str = typer.Option("owner", help="Device role: owner | controller | observer."),
    label: str = typer.Option("", help="Suggested device label (e.g. 'Almir's iPhone 15 Pro')."),
) -> None:
    """Mint a one-time iPhone pairing code (on the host). Type it — or scan it — into a fresh Sali app.

    The code is single-use and expires quickly; it never becomes a permanent credential. Only its hash is
    stored. Run this on the machine where Sali lives, then enter the code in the app's enrollment screen."""
    import asyncio

    from sali.api.devices import DeviceStore
    from sali.kernel import Kernel

    settings = load_settings()
    configure_logging("WARNING")

    async def _mint() -> None:
        kernel = Kernel.create(settings)
        try:
            pool = await kernel.pool()
            code, expires_at = await DeviceStore(pool).mint_enrollment_code(
                role=role, label=(label or None), created_by="host-cli")
            console.print(f"\n  Pairing code: [bold cyan]{code}[/]")
            console.print(f"  Role:         {role}")
            console.print(f"  Expires:      {expires_at.isoformat()}\n")
            console.print("  Enter this in the Sali app's enrollment screen. Single-use; expires soon.\n")
        finally:
            await kernel.close()

    asyncio.run(_mint())


@app.command("change-password")
def change_password(
    new: str = typer.Argument(None, help="The new API login password (omit to be prompted securely)."),
) -> None:
    """Set or rotate the password the iPhone app logs in with (password auth).

    The password is the DURABLE credential: the app re-authenticates with it whenever a token expires, so an
    expired session can never lock you out while you're away from this machine. Rotating it REVOKES every
    live session immediately — so this is also the "log everyone out" switch if a phone is lost. Stored
    one-way (salted scrypt), never in plaintext. Run this on the machine where Sali lives."""
    import asyncio

    from sali.api.devices import DeviceStore
    from sali.api.password import hash_password
    from sali.kernel import Kernel

    settings = load_settings()
    configure_logging("WARNING")

    if not new:
        new = typer.prompt("New API password", hide_input=True, confirmation_prompt=True)
    new = (new or "").strip()
    if len(new) < 4:
        raise typer.BadParameter("password must be at least 4 characters")

    async def _set() -> None:
        kernel = Kernel.create(settings)
        try:
            pool = await kernel.pool()
            await DeviceStore(pool).set_password(hash_password(new))
        finally:
            await kernel.close()

    asyncio.run(_set())
    console.print("\n  [green]Password changed.[/] Every device is logged out — the app will ask for the "
                  "new password next time it needs one.\n")


@app.command("push-test")
def push_test(
    message: str = typer.Option("Test push from Sali.", help="What the banner should say."),
    title: str = typer.Option("Sali", help="The banner's title."),
) -> None:
    """Send a REAL APNs push to every enrolled iPhone that has registered a token (§11).

    The one honest way to answer "do banners actually arrive": it signs a provider token with the
    configured .p8 and talks to Apple, then prints what Apple said. Unconfigured, it says which
    piece is missing rather than reporting a success it cannot have had.
    """
    import asyncio

    from sali.api.push import ApnsCredentials, ApnsSender, ApnsUnavailable
    from sali.kernel import Kernel

    settings = load_settings()
    configure_logging("WARNING")

    async def _send() -> int:
        try:
            creds = ApnsCredentials.resolve(settings)
        except ApnsUnavailable as exc:
            console.print(f"\n  [yellow]Push is not configured[/] — {exc}\n")
            return 1

        kernel = Kernel.create(settings)
        try:
            pool = await kernel.pool()
            sender = ApnsSender(pool, creds)
            try:
                result = await sender.send(title=title, body=message)
            except ApnsUnavailable as exc:
                console.print(f"\n  [yellow]Push cannot be attempted[/] — {exc}\n")
                return 1
            finally:
                await sender.aclose()
        finally:
            await kernel.close()

        if result.sent == 0 and result.failed == 0:
            console.print("\n  [yellow]No enrolled device has registered a push token.[/]")
            console.print("  Open the app → Sali → Settings & security → Notifications → "
                          "Allow notifications.\n")
            return 1
        console.print(f"\n  Delivered to Apple: [bold]{result.sent}[/]  ·  "
                      f"failed: {result.failed}  ·  forgotten dead tokens: {result.pruned}")
        if result.reasons:
            console.print(f"  Apple said: {', '.join(sorted(set(result.reasons)))}")
        console.print()
        return 0 if result.sent else 1

    raise typer.Exit(code=asyncio.run(_send()))


def _fmt_tool(data: dict[str, Any]) -> str:
    args = data.get("args") or {}
    value = args.get("command") or args.get("path") or args.get("repo")
    if value is None and args:
        value = next(iter(args.values()), "")
    return str(value or "")[:70]


async def _stream_turn(loop: Any, text: str, session: UUID,
                       renderer: Any = None) -> None:
    """Render one turn as a flowing transcript with the advanced renderer.

    Combines Rich Live for streaming tokens with the TerminalRenderer for tools,
    memory, and status events. Append-only — nothing already shown is wiped.
    """
    from sali.cli.renderer import S, TerminalRenderer

    if renderer is None:
        renderer = TerminalRenderer(console)

    # Subtle separator between turns (the prompt already showed the user's input)
    console.print()
    renderer.start_turn()
    turn_start = time.monotonic()
    tool_calls = 0
    err: Exception | None = None

    # Streaming state
    seg = ""           # current tokens, not yet committed
    activity = "processing…"
    thinking_text = ""
    is_working = True  # always show spinner while Sali is working

    from rich.console import Group
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.spinner import Spinner
    from rich.text import Text

    spinner = Spinner("dots", style="cyan")

    def render_frame() -> Group:
        parts: list[Any] = []
        if seg:
            from sali.cli.renderer import C as RC
            from sali.cli.renderer import S
            parts.append(Text.assemble(
                (f"  {S.PROMPT_SALI} ", RC.SALI_PREFIX),
            ))
            parts.append(Markdown(seg.rstrip()))
        if thinking_text and not seg:
            from sali.cli.renderer import C as RC
            from sali.cli.renderer import S
            preview = thinking_text[:150] + ("…" if len(thinking_text) > 150 else "")
            parts.append(Text.assemble(
                (f"  {S.THINKING} ", RC.THINKING),
                (preview, f"dim {RC.THINKING}"),
            ))
        # Always show spinner while working — gives visual feedback that Sali is active
        if is_working:
            display_activity = activity or "processing…"
            spinner.update(text=Text(f" {display_activity}", style="dim cyan"))
            parts.append(Text("  "))
            parts.append(spinner)
        return Group(*parts)

    with Live(render_frame(), console=console, refresh_per_second=15, transient=False) as live:
        # Let confirmer pause the spinner for y/N prompts
        confirmer = getattr(loop, "confirmer", None)
        if confirmer is not None and hasattr(confirmer, "attach"):
            confirmer.attach(live)

        def commit() -> None:
            nonlocal seg
            if seg.strip():
                from sali.cli.renderer import C as RC
                from sali.cli.renderer import S
                live.console.print(Text.assemble(
                    (f"  {S.PROMPT_SALI} ", RC.SALI_PREFIX),
                ))
                live.console.print(Markdown(seg.rstrip()))
            seg = ""

        try:
            async for event in loop.astream(text, session_id=session):
                if event.kind == "token":
                    seg += event.text
                    activity = "writing…"
                elif event.kind == "status":
                    activity = f"{event.text}…"
                elif event.kind == "thinking":
                    thinking_text += event.text
                    activity = "thinking…"
                elif event.kind == "tool":
                    if event.data.get("phase") == "start":
                        commit()
                        tool_calls += 1
                        name = event.data.get("name", "?")
                        args = event.data.get("args")
                        from sali.cli.renderer import _fmt_args
                        activity = f"{name} {_fmt_args(args)}".strip()
                    else:
                        ok = event.data.get("ok", True)
                        summary = str(event.data.get("summary") or event.data.get("name", ""))
                        from sali.cli.renderer import C as RC
                        from sali.cli.renderer import S
                        mark = f"[{RC.TOOL_OK}]{S.TOOL_OK}[/]" if ok else f"[{RC.TOOL_FAIL}]{S.TOOL_FAIL}[/]"
                        live.console.print(f"  {mark} [{RC.DIM}]{summary}[/]")
                        activity = "working…"
                        thinking_text = ""
                elif event.kind == "retrieval":
                    count = len(event.data.get("memories", []))
                    if count:
                        commit()
                        renderer.render_memory_recall(count, event.data.get("query"))
                elif event.kind == "final":
                    if event.text and not seg.strip():
                        seg = event.text
                    activity = "done"
                live.update(render_frame())
        except Exception as exc:  # noqa: BLE001 - re-raised after the last frame
            err = exc
        finally:
            is_working = False  # stop the spinner
            activity = ""
            live.update(render_frame())

    duration = time.monotonic() - turn_start
    renderer.render_turn_summary(tool_calls, duration)
    if err is not None:
        raise err


async def _stream_turn_via_runtime(
    runtime: Any, text: str, renderer: Any, origin: Any,
) -> None:
    """Submit a turn through the runtime coordinator with streaming terminal rendering.

    This is the unified execution path: terminal submits through the coordinator
    (same as API), but renders events in the terminal as they arrive.
    """
    from sali.cli.renderer import TerminalRenderer

    if renderer is None:
        renderer = TerminalRenderer(console)

    console.print()
    renderer.start_turn()
    turn_start = time.monotonic()
    tool_calls = 0
    err: Exception | None = None

    seg = ""
    activity = "processing…"
    thinking_text = ""
    is_working = True

    from rich.console import Group
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.spinner import Spinner
    from rich.text import Text

    spinner = Spinner("dots", style="cyan")

    def render_frame() -> Group:
        parts: list[Any] = []
        if seg:
            from sali.cli.renderer import C as RC
            from sali.cli.renderer import S
            parts.append(Text.assemble(
                (f"  {S.PROMPT_SALI} ", RC.SALI_PREFIX),
            ))
            parts.append(Markdown(seg.rstrip()))
        if thinking_text and not seg:
            from sali.cli.renderer import C as RC
            from sali.cli.renderer import S
            preview = thinking_text[:150] + ("…" if len(thinking_text) > 150 else "")
            parts.append(Text.assemble(
                (f"  {S.THINKING} ", RC.THINKING),
                (preview, f"dim {RC.THINKING}"),
            ))
        if is_working:
            display_activity = activity or "processing…"
            spinner.update(text=Text(f" {display_activity}", style="dim cyan"))
            parts.append(Text("  "))
            parts.append(spinner)
        return Group(*parts)

    # Event queue: the coordinator's event_callback puts events here,
    # and the Live display consumes them.
    import asyncio
    event_queue: asyncio.Queue[Any] = asyncio.Queue()

    def on_event(event: Any) -> None:
        """Called by the coordinator for each LoopEvent."""
        event_queue.put_nowait(event)

    with Live(render_frame(), console=console, refresh_per_second=15, transient=False) as live:
        confirmer = getattr(runtime.loop, "confirmer", None)
        if confirmer is not None and hasattr(confirmer, "attach"):
            confirmer.attach(live)

        def commit() -> None:
            nonlocal seg
            if seg.strip():
                from sali.cli.renderer import C as RC
                from sali.cli.renderer import S
                live.console.print(Text.assemble(
                    (f"  {S.PROMPT_SALI} ", RC.SALI_PREFIX),
                ))
                live.console.print(Markdown(seg.rstrip()))
            seg = ""

        async def _consume_events() -> None:
            """Consume events from the queue and render them."""
            nonlocal seg, activity, thinking_text, tool_calls, is_working, err
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.5)
                except TimeoutError:
                    if not is_working:
                        break
                    continue

                if event is None:  # sentinel: stream done
                    break

                if event.kind == "token":
                    seg += event.text
                    activity = "writing…"
                elif event.kind == "status":
                    activity = f"{event.text}…"
                elif event.kind == "thinking":
                    thinking_text += event.text
                    activity = "thinking…"
                elif event.kind == "tool":
                    if event.data.get("phase") == "start":
                        commit()
                        tool_calls += 1
                        name = event.data.get("name", "?")
                        args = event.data.get("args")
                        from sali.cli.renderer import _fmt_args
                        activity = f"{name} {_fmt_args(args)}".strip()
                    else:
                        ok = event.data.get("ok", True)
                        summary = str(event.data.get("summary") or event.data.get("name", ""))
                        from sali.cli.renderer import C as RC
                        from sali.cli.renderer import S
                        mark = f"[{RC.TOOL_OK}]{S.TOOL_OK}[/]" if ok else f"[{RC.TOOL_FAIL}]{S.TOOL_FAIL}[/]"
                        live.console.print(f"  {mark} [{RC.DIM}]{summary}[/]")
                        activity = "working…"
                        thinking_text = ""
                elif event.kind == "retrieval":
                    count = len(event.data.get("memories", []))
                    if count:
                        commit()
                        renderer.render_memory_recall(count, event.data.get("query"))
                elif event.kind == "final":
                    if event.text and not seg.strip():
                        seg = event.text
                    activity = "done"
                live.update(render_frame())

        try:
            # Route through the attention authority (Prompt 2): classify → answer / interrupt+suspend
            # → do the work → auto-resume the primary. All foreground still serialized via the lease.
            submit_task = asyncio.create_task(
                runtime.handle_message(text, origin=origin.value, event_callback=on_event))
            consume_task = asyncio.create_task(_consume_events())

            # Wait for both: submit completes the turn, consume renders events
            result = await submit_task
            # Signal the consumer that the stream is done
            await event_queue.put(None)
            await consume_task

            if result.get("status") == "error":
                err = Exception(result.get("error", "unknown error"))
        except Exception as exc:  # noqa: BLE001
            err = exc
        finally:
            is_working = False
            activity = ""
            live.update(render_frame())

    duration = time.monotonic() - turn_start
    renderer.render_turn_summary(tool_calls, duration)
    if err is not None:
        raise err


async def _daemon_reachable(base: str, *, timeout: float = 10.0, attempts: int = 3) -> bool:
    """Is a Sali API actually answering at this base URL?

    Patient on purpose. The first version used a single 2s probe, and the machine proved why that is
    wrong: while Sali is prefilling a long prompt, every core is busy and his event loop can take
    seconds to answer a health check. A short probe reads that as "no Sali here" — which is the one
    conclusion that must never be reached by guessing, since it is the doorway to a second mind.
    """
    import httpx
    for i in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                if (await c.get(f"{base}/healthz")).status_code == 200:
                    return True
        except Exception:  # noqa: BLE001 - a busy Sali looks exactly like an absent one; retry
            pass
        if i + 1 < attempts:
            await asyncio.sleep(1.0)
    return False


async def _attach_to_living_sali(message: str | None) -> bool:
    """If Sali is alive, become a window onto him and run the conversation there. True if attached.

    The mind lock — not a port probe — is what decides whether he lives, and it carries the endpoint
    he published when his API bound. A living mind with no API is a real state (a standalone
    `sali agent` in another terminal): we refuse to become a second Sali and say where he is.
    """
    set_current_role(ProcessRole.CLIENT)
    holder = live_holder()
    if holder is None:
        return False
    base = holder.api_base
    if base is None:
        # He lives, but serves no door: a standalone `sali agent` in another terminal.
        console.print("[yellow]Sali is already alive on this machine, and serves no API.[/]")
        console.print(f"[dim]{describe_holder(holder)}[/]")
        console.print("[dim]He is a standalone terminal session — talk to him in that window, or "
                      "give him a permanent life every window can reach: "
                      "[bold]sudo systemctl start sali[/].[/]")
        raise typer.Exit(1)
    if not await _daemon_reachable(base):
        # He published a door that is not answering. Almost always: he is deep in a long prefill and
        # his event loop is starved. Say what is true and stop — becoming a second mind here would
        # put a second 16GB model on a 12GB card, which is how this machine powers itself off.
        console.print(f"[yellow]Sali is alive but his API at {base} did not answer.[/]")
        console.print(f"[dim]{describe_holder(holder)}[/]")
        console.print("[dim]He is most likely mid-thought (a long prompt saturates every core). "
                      "Try again in a moment, or watch him: [bold]journalctl -u sali -f[/]. "
                      "Not starting a second Sali.[/]")
        raise typer.Exit(1)
    await _agent_via_daemon(base, message)
    return True


async def _agent_via_daemon(base: str, message: str | None) -> None:
    """Thin client: talk to the ALREADY-RUNNING Sali daemon over its local API and stream the reply. This
    process loads NO model — it is a window into the one running mind (Almir's rule: one Sali, one model)."""
    import json

    import httpx
    import websockets

    from sali.api.auth import get_or_create_token
    token = get_or_create_token()  # host identity (owner) — the local break-glass credential
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?token={token}"
    headers = {"Authorization": f"Bearer {token}"}

    console.print("[dim]Attached to the running Sali (one mind, no second model). Ctrl-C to exit.[/]\n")

    async def one_turn(text: str) -> None:
        async with websockets.connect(ws_url, open_timeout=10, max_size=None) as ws:
            # subscribe past the current tail so we stream only THIS turn's events (single owner, one turn)
            await ws.send(json.dumps({"type": "subscribe", "after_seq": 2**62}))
            async with httpx.AsyncClient(timeout=30.0) as c:
                resp = await c.post(f"{base}/api/v1/conversation/message",
                                    json={"content": text}, headers=headers)
                if resp.status_code == 403:
                    console.print("[red]This device is read-only.[/]")
                    return
                resp.raise_for_status()
            console.print("[bold cyan]sali[/] ", end="")
            streamed = False
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=600)
                except (TimeoutError, websockets.ConnectionClosed):
                    break
                msg = json.loads(raw)
                if msg.get("type") != "event":
                    continue
                et, data = msg.get("event_type", ""), (msg.get("data") or {})
                if et == "agent.token":
                    console.print(data.get("text", ""), end="", markup=False)
                    streamed = True
                elif et in ("agent.tool", "task.tool.started"):
                    console.print(f"\n[dim]· {data.get('text') or data.get('tool') or 'working'}[/]")
                elif et == "agent.final":
                    if not streamed and data.get("text"):
                        console.print(data.get("text", ""), end="", markup=False)
                    break
                elif et == "error":
                    console.print(f"\n[red]{data.get('error', 'error')}[/]")
                    break
            console.print("\n")

    if message:
        await one_turn(message)
        return
    while True:
        try:
            text = console.input("[bold]you[/] ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not text.strip():
            continue
        try:
            await one_turn(text)
        except KeyboardInterrupt:
            console.print("\n[dim]interrupted[/]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]{type(exc).__name__}: {str(exc)[:160]}[/]")


async def _agent(settings: Settings, message: str | None) -> None:
    from sali.cli.renderer import C, S, TerminalRenderer
    from sali.core.mind import SecondMindError
    from sali.db.bootstrap import init_database
    from sali.kernel import Kernel
    from sali.runtime.coordinator import ExecutionOrigin
    from sali.security.confirm import TerminalConfirmer

    # ── Window first ─────────────────────────────────────────────────────────────────────────────
    # Is Sali already alive on this machine? The mind lock is the authority — not a probe of a
    # hardcoded port, which would miss a daemon on another port and quietly start a second Sali. The
    # lock also tells us WHERE he listens, so discovery and exclusion are the same fact.
    if await _attach_to_living_sali(message):
        return

    # ── Nobody home: become the one Sali for as long as this terminal runs ───────────────────────
    await init_database(settings)  # idempotent: apply any outstanding migrations
    try:
        kernel = await Kernel.become_mind(settings)
    except SecondMindError as exc:
        # Lost a start-up race with a daemon or another terminal. Try once more to attach; if that
        # mind serves no API, say so plainly rather than becoming a second one.
        if await _attach_to_living_sali(message):
            return
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(1) from None
    console.print("[dim](this terminal is the one Sali — no daemon was running. "
                  "`sudo systemctl start sali` gives him a permanent life every window can reach.)[/]")
    runtime = await kernel.runtime(confirmer=TerminalConfirmer())
    renderer = TerminalRenderer(console)

    # Print header
    renderer.print_header(__version__, settings.model.chat_model, str(runtime.session_id))

    # Check for recovery
    try:
        recovered = await runtime.recover_tasks()
        renderer.render_recovery_info(recovered)
    except Exception:  # noqa: BLE001 - recovery is best-effort
        pass

    try:
        if message:
            await _stream_turn_via_runtime(runtime, message, renderer, ExecutionOrigin.CLI)
            return
        reader = _live_reader(runtime.loop)
        while True:
            try:
                text = await reader.prompt(f"  {S.PROMPT_USER} ") if reader is not None \
                    else console.input(f"[{C.USER_PROMPT}]{S.PROMPT_USER}[/] ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                return
            if not text.strip():
                continue
            try:
                await _stream_turn_via_runtime(runtime, text, renderer, ExecutionOrigin.CLI)
            except KeyboardInterrupt:
                console.print(f"\n  [{C.WARNING}]interrupted[/]")
            except Exception as exc:
                renderer.render_error(str(exc)[:200], context=type(exc).__name__)
    finally:
        # kernel.close() tears the runtime down and releases machine-wide ownership LAST, so the
        # next `sali agent` finds the lock free rather than a corpse.
        await kernel.close()


def _live_reader(loop: Any) -> Any:
    """The keystroke-aware reader, so Sali senses as Almir types. Returns None (→ a plain
    blocking prompt) when there's no real terminal or prompt_toolkit is missing — the sensing
    is a nicety, never a requirement."""
    import sys

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        from sali.cli.liveinput import SenseInput
    except Exception:  # noqa: BLE001 - degrade to the ordinary blocking prompt
        return None
    return SenseInput(loop.sense)


@app.command()
def recover() -> None:
    """Resolve any agent runs interrupted by a crash (surfaces them; never silent-retries).

    Crash recovery is something the mind does to ITSELF at startup. Running it from outside a living
    Sali would reclaim the runs he is in the middle of — the command reads as read-only and is not.
    """
    settings = load_settings()
    configure_logging("WARNING")
    # A live mind already recovered its own runs when it started, and is the only one entitled to
    # judge which of its runs are stale. Reclaiming them from here would abort work in flight.
    _refuse_if_mind_alive("recover", suggest="sali runs  # see the runs he already resolved")
    asyncio.run(_recover(settings))


async def _recover(settings: Settings) -> None:
    from sali.core.mind import SecondMindError
    from sali.kernel import Kernel
    from sali.security.confirm import AutoDenyConfirmer

    # Recovery drives the agent loop, so it is the mind for as long as it runs — never a second one.
    try:
        kernel = await Kernel.become_mind(settings)
    except SecondMindError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(1) from None
    loop = await kernel.agent_loop(confirmer=AutoDenyConfirmer())
    try:
        resolved = await loop.recover()
        if not resolved:
            console.print("[green]No interrupted runs.[/]")
        for entry in resolved:
            console.print(
                f"run {entry['run_id'][:8]} was in [yellow]{entry['was_state']}[/] "
                f"→ [bold]{entry['action']}[/]"
            )
    finally:
        await kernel.close()


@app.command()
def runs(last: int = typer.Option(10, help="How many recent runs to show.")) -> None:
    """List recent agent runs (status, tool count, and the prompt)."""
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_runs(settings, last))


async def _runs(settings: Settings, last: int) -> None:
    from sali.db.pool import connect

    conn = await connect(settings)
    try:
        rows = await conn.fetch(
            "SELECT r.run_id, r.status, r.state, r.iteration, r.started_at, "
            "  (SELECT count(*) FROM tool_execution t WHERE t.run_id=r.run_id) AS tools, "
            "  left(r.user_input, 60) AS input "
            "FROM agent_runs r ORDER BY r.started_at DESC LIMIT $1",
            last,
        )
    finally:
        await conn.close()
    table = Table(title="recent runs")
    for col in ("when", "status", "state", "iters", "tools", "input"):
        table.add_column(col)
    for row in rows:
        table.add_row(
            f"{row['started_at']:%m-%d %H:%M}", row["status"], row["state"],
            str(row["iteration"]), str(row["tools"]), row["input"] or "",
        )
    console.print(table)


@app.command(name="run")
def show_run(run_id: str = typer.Argument(..., help="A run id (or its first characters).")) -> None:
    """Show the journaled event trace of one agent run."""
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_show_run(settings, run_id))


async def _show_run(settings: Settings, run_id: str) -> None:
    from sali.db.pool import connect

    conn = await connect(settings)
    try:
        run = await conn.fetchrow(
            "SELECT run_id, status, state FROM agent_runs WHERE run_id::text LIKE $1 LIMIT 1",
            run_id + "%",
        )
        if run is None:
            console.print(f"[red]no run matching {run_id}[/]")
            return
        events = await conn.fetch(
            "SELECT seq, kind, payload, latency_ms FROM run_events WHERE run_id=$1 ORDER BY seq",
            run["run_id"],
        )
    finally:
        await conn.close()
    console.print(f"run [bold]{str(run['run_id'])[:8]}[/] — {run['status']} ({run['state']})")
    for event in events:
        extra = f" [dim]{event['latency_ms']}ms[/]" if event["latency_ms"] else ""
        console.print(f"  {event['seq']:>2} [cyan]{event['kind']}[/]{extra}  {event['payload']}")


@app.command()
def remember(
    text: str = typer.Argument(..., help="A fact to teach Sali."),
    layer: str = typer.Option("semantic", help="Memory layer (semantic/preference/episodic/identity)."),
) -> None:
    """Teach Sali a fact — persisted with provenance (user_explicit) and embedded for recall."""
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_remember(settings, text, layer))


async def _remember(settings: Settings, text: str, layer: str) -> None:
    from sali.core.enums import MemoryLayer, MemorySource
    from sali.db.pool import create_pool
    from sali.memory.service import MemoryService
    from sali.provider.registry import build_provider

    pool = await create_pool(settings)
    provider = build_provider(settings)
    service = MemoryService(pool, provider)
    try:
        memory = await service.remember(
            layer=MemoryLayer(layer), content=text, source=MemorySource.USER_EXPLICIT
        )
        await service.embed_pending()
        console.print(f"[green]Remembered[/] (confidence {memory.confidence:.2f}): {text}")
    finally:
        await pool.close()


@app.command()
def recall(query: str = typer.Argument(..., help="What to look up.")) -> None:
    """Show what Sali retrieves for a query — memories (with confidence) and graph relationships."""
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_recall(settings, query))


async def _recall(settings: Settings, query: str) -> None:
    from sali.db.pool import create_pool
    from sali.provider.registry import build_provider
    from sali.retrieval.router import classify
    from sali.retrieval.service import RetrievalService

    pool = await create_pool(settings)
    provider = build_provider(settings)
    service = RetrievalService(pool, provider,
                               owner_timezone=settings.temporal.owner_timezone)
    try:
        plan = classify(query)
        bundle = await service.gather(query, plan, k=6)
        console.print(f"[dim]intent={plan.intent} · needs_live={plan.needs_live}[/]")
        if not bundle.memories and not bundle.graph_facts:
            console.print("[dim]Nothing relevant stored yet — teach Sali with `sali remember`.[/]")
        for hit in bundle.memories:
            tag = f"conf {hit.effective_confidence:.2f}" + (" · STALE" if hit.stale else "")
            console.print(f"[cyan]memory[/] ([dim]{tag}[/]) {hit.memory.content}")
        for fact in bundle.graph_facts:
            console.print(f"[magenta]graph[/]  {fact.src} --{fact.rel}--> {fact.dst}")
    finally:
        await pool.close()


@app.command()
def learn(daily: bool = False) -> None:
    """Consolidate what Sali has done into knowledge — learn repeated procedures, record failures.

    Learning is memory acquisition, not retraining (§17-19). A procedure is only learned once it
    has repeated evidence — never from a single observation.

    With --daily, run the idempotent daily consolidation cycle (Prompt 7 §9/§26): promote learning
    candidates that cleared the evidence bar, retire failed experiments, surface contradictions.
    Safe to run repeatedly and from cron/systemd — one cycle per day, with bounded catch-up for
    missed days.
    """
    settings = load_settings()
    configure_logging("WARNING")
    _refuse_if_mind_alive("learn", suggest="sali agent  # his daily consolidation runs inside him")
    asyncio.run(_learn_daily(settings) if daily else _learn(settings))


async def _learn_daily(settings: Settings) -> None:
    from sali.db.pool import create_pool
    from sali.events.publisher import EventPublisher
    from sali.learning.daily import DailyConsolidation
    from sali.learning.service import LearningService
    from sali.provider.registry import build_provider

    pool = await create_pool(settings)
    provider = build_provider(settings)
    daily = DailyConsolidation(pool, provider, EventPublisher(pool),
                               learning_service=LearningService(pool, provider))
    try:
        with console.status("[cyan]running the daily learning cycle…[/]"):
            summaries = await daily.catch_up(max_cycles=3)
        for s in summaries:
            if s.skipped:
                console.print(f"[dim]{s.ran_on}: already consolidated today (idempotent).[/]")
            else:
                console.print(
                    f"[green]{s.ran_on}:[/] promoted {s.promoted}, archived {s.archived}, "
                    f"{s.candidates_active} active lesson(s), {s.contradictions_open} open "
                    f"contradiction(s), {s.behavior_pending} behavior proposal(s) pending.")
    finally:
        await pool.close()


async def _learn(settings: Settings) -> None:
    from sali.db.pool import create_pool
    from sali.learning.service import LearningService
    from sali.provider.registry import build_provider

    pool = await create_pool(settings)
    service = LearningService(pool, build_provider(settings))
    try:
        with console.status("[cyan]consolidating what you've been doing…[/]"):
            result = await service.consolidate()
        if result.procedures:
            console.print(f"[green]Learned {len(result.procedures)} procedure(s):[/]")
            for p in result.procedures:
                console.print(f"  • [bold]{p.name}[/] [dim](seen in {p.evidence} runs)[/]: "
                              + " → ".join(p.steps))
        if result.failures_recorded:
            console.print(f"[yellow]Noted {result.failures_recorded} past failure(s)[/] to learn from.")
        if result.episodes_created:
            console.print("[green]Folded recent activity into an episode.[/]")
        if result.stm_pruned:
            console.print(f"[dim]Pruned {result.stm_pruned} stale short-term observation(s).[/]")
        if not result.did_something:
            console.print("[dim]Nothing new to consolidate — Sali learns from repeated activity.[/]")
    finally:
        await pool.close()


@app.command()
def schedules() -> None:
    """List Sali's recurring schedules (§44)."""
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_schedules(settings))


async def _schedules(settings: Settings) -> None:
    from sali.db.pool import create_pool
    from sali.scheduler.store import ScheduleStore

    pool = await create_pool(settings)
    try:
        rows = await ScheduleStore(pool).list_all()
    finally:
        await pool.close()
    if not rows:
        console.print("[dim]No schedules. Ask Sali to schedule something (it fires as a full turn).[/]")
        return
    for s in rows:
        nxt = f" · next {s.next_run_at:%Y-%m-%d %H:%M}" if s.enabled else ""
        console.print(f"• {s.one_line()}[dim]{nxt}[/]")


@app.command()
def scheduler(
    interval: int = typer.Option(30, help="Seconds between checks for due schedules."),
) -> None:
    """Run the scheduler: fire due schedules as unattended Sali turns (§44). Ctrl-C stops."""
    settings = load_settings()
    configure_logging("WARNING")
    # The daemon already fires due schedules as one of Sali's faculties; a second scheduler process
    # would be a second mind driving the same task graph.
    _refuse_if_mind_alive("scheduler", suggest="sali schedules  # see what is due")
    asyncio.run(_scheduler(settings, interval))


async def _scheduler(settings: Settings, interval: int) -> None:
    from sali.core.mind import SecondMindError
    from sali.kernel import Kernel
    from sali.scheduler.daemon import SchedulerDaemon
    from sali.scheduler.store import ScheduleStore
    from sali.security.confirm import AutoDenyConfirmer

    # Running only the scheduler is running a Sali with one faculty — so it becomes THE mind, or it
    # does not run. (`sali daemon` is the same organism with all of them; prefer it.)
    try:
        kernel = await Kernel.become_mind(settings)
    except SecondMindError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(1) from None
    # Scheduled work goes through submit_background() — the same one AgentLoop, arbitrated by the
    # same coordinator, yielding to Almir the moment he speaks.
    runtime = await kernel.runtime(confirmer=AutoDenyConfirmer())
    pool = await kernel.pool()
    # Scheduled work uses the BACKGROUND session — never writes to Almir's conversation.
    session = background_session_id()

    class _LoopRunner:
        async def run(self, prompt: str) -> Any:
            return await runtime.submit_background(prompt, session_id=session)

    daemon = SchedulerDaemon(ScheduleStore(pool), _LoopRunner(), pool=pool, poll_s=float(interval))
    console.print(f"[dim]scheduler running (checking every {interval}s) — Ctrl-C to stop[/]")
    task = asyncio.create_task(daemon.run_forever())
    try:
        await task
    except (KeyboardInterrupt, asyncio.CancelledError):
        daemon.stop()
    finally:
        await kernel.close()   # tears down the runtime AND releases machine-wide ownership


@app.command()
def tasks() -> None:
    """Show the persistent tasks Sali has in progress (they survive restarts)."""
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_tasks(settings))


async def _tasks(settings: Settings) -> None:
    from sali.db.pool import create_pool
    from sali.tasks.store import TaskStore

    pool = await create_pool(settings)
    try:
        open_tasks = await TaskStore(pool).open_tasks(limit=20)
    finally:
        await pool.close()
    if not open_tasks:
        console.print("[dim]No tasks in progress.[/]")
        return
    for t in open_tasks:
        console.print(f"[bold]{t.objective}[/] [dim]({t.status})[/]")
        for step in t.steps:
            console.print(f"   {step.seq}. [{step.status}] {step.description}")


@app.command("email-auth")
def email_auth() -> None:
    """Authorize Gmail sending over the HTTPS API (for networks that block SMTP). One-time OAuth."""
    import contextlib
    import http.server
    import threading
    import time
    import urllib.parse
    import webbrowser

    from sali.comms.gmail_api import authorization_url, exchange_code
    from sali.config.secrets import SecretStore

    secrets = SecretStore()
    client_id, client_secret = secrets.get("gmail.client_id"), secrets.get("gmail.client_secret")
    if not client_id or not client_secret:
        console.print("[yellow]First store your OAuth client:[/]")
        console.print("  sali secrets set gmail.client_id\n  sali secrets set gmail.client_secret")
        console.print("[dim]Create one at console.cloud.google.com → APIs & Services → Credentials → "
                      "OAuth client ID → Desktop app, and enable the Gmail API for the project.[/]")
        return

    captured: dict[str, str] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured["code"] = params.get("code", [""])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Sali is authorized to send email. You can close this tab.</h2>")

        def log_message(self, *args: object) -> None:  # silence the server's stderr logging
            pass

    server = http.server.HTTPServer(("localhost", 0), _Handler)
    redirect_uri = f"http://localhost:{server.server_address[1]}"
    url = authorization_url(client_id, redirect_uri)
    console.print(f"[bold]Open this URL and grant Sali send access:[/]\n{url}\n")
    with contextlib.suppress(Exception):
        webbrowser.open(url)
    threading.Thread(target=server.handle_request, daemon=True).start()
    for _ in range(300):  # wait up to 5 min for the redirect
        if captured.get("code"):
            break
        time.sleep(1)
    server.server_close()
    if not captured.get("code"):
        console.print("[red]No authorization code received — try again.[/]")
        return
    refresh = asyncio.run(exchange_code(client_id, client_secret, captured["code"], redirect_uri))
    secrets.set("gmail.refresh_token", refresh)
    console.print("[green]✓ Gmail sending authorized.[/] Set [bold]send_backend = 'gmail_api'[/] under "
                  "[comms.mail] in ~/.config/sali/sali.toml, and email will send over HTTPS.")


@app.command()
def ingest(path: str) -> None:
    """Read a document into Sali's memory so it can recall and cite it (§44)."""
    settings = load_settings()
    configure_logging("WARNING")
    _refuse_if_mind_alive("ingest", suggest="sali agent  # ask him to read the document")
    asyncio.run(_ingest(settings, path))


async def _ingest(settings: Settings, path: str) -> None:
    from sali.db.pool import create_pool
    from sali.ingest.service import IngestService
    from sali.provider.registry import build_provider

    pool = await create_pool(settings)
    try:
        result = await IngestService(pool, build_provider(settings)).ingest(path)
    finally:
        await pool.close()
    if result.status == "ok":
        console.print(f"[green]ingested[/] {result.chunks} chunk(s) from {path}")
    elif result.status == "unchanged":
        console.print(f"[dim]unchanged — {path} was already ingested[/]")
    else:
        console.print(f"[yellow]{result.status}[/] — {result.detail or path}")


@app.command()
def procedures() -> None:
    """List the procedures Sali has learned from watching Almir work."""
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_procedures(settings))


async def _procedures(settings: Settings) -> None:
    from sali.db.pool import create_pool
    from sali.learning.service import LearningService
    from sali.provider.registry import build_provider

    pool = await create_pool(settings)
    try:
        procs = await LearningService(pool, build_provider(settings)).procedures()
    finally:
        await pool.close()
    if not procs:
        console.print("[dim]No procedures learned yet — run `sali learn` after repeating a workflow.[/]")
        return
    for p in procs:
        ev = f" · seen {p['evidence']}×" if p.get("evidence") else ""
        console.print(f"• {p['content']}  [dim](conf {p['confidence']:.2f}{ev})[/]")


@app.command()
def twin(
    refresh: bool = typer.Option(False, "--refresh", help="Re-observe the machine before showing."),
) -> None:
    """Show Sali's desktop digital twin — its structural picture of this machine (§14).

    With --refresh, run the deterministic observers first and fold what they find into the graph.
    """
    settings = load_settings()
    configure_logging("WARNING")
    if refresh:  # a refresh interprets observations with the model — that is the mind's work
        _refuse_if_mind_alive("twin --refresh", suggest="sali twin  # read it without refreshing")
    asyncio.run(_twin(settings, refresh))


def _twin_service(pool: Any, settings: Settings, provider: Any = None) -> Any:
    """A TwinService wired with a memory service, so a refresh also records the machine as
    retrievable system-env facts (grounding "what GPU / how much RAM / what's installed").

    `provider` lets the mind pass its OWN provider handle in, so the daemon's twin faculty shares the
    one inference path instead of constructing a parallel one."""
    from sali.memory.service import MemoryService
    from sali.twin.service import TwinService

    if provider is None:
        from sali.provider.registry import build_provider
        provider = build_provider(settings)
    return TwinService(pool, memory=MemoryService(pool, provider), settings=settings)


async def _twin(settings: Settings, refresh: bool) -> None:
    from sali.db.pool import create_pool

    pool = await create_pool(settings)
    service = _twin_service(pool, settings)
    try:
        if refresh:
            with console.status("[cyan]observing the machine…[/]"):
                result = await service.refresh(
                    exclude_projects=tuple(settings.permissions.fs_deny)  # never scan off-limits dirs
                )
            note = f"{result.entities} things"
            if result.added:
                note += f" · +{len(result.added)} new"
            if result.removed:
                note += f" · -{len(result.removed)} gone"
            console.print(f"[green]twin refreshed[/] — {note}")
        console.print((await service.tree()).replace("[", "\\["))
    finally:
        await pool.close()


_SERVICE_NAME = "sali-observe.service"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _SERVICE_NAME


def _install_observe_service(interval: int) -> None:
    """RETIRED. Installing this unit was how the machine ended up with two Salis.

    `sali-observe.service` ran a second OS process with its own pool, its own provider and its own
    TwinDaemon + LearningService — a background presence beside the daemon's, driving the model
    against the same database. Observation is not a separate program; it is one of the living Sali's
    faculties, and `sali daemon` already runs it.
    """
    console.print("[yellow]`sali observe --install` is retired — it installed a SECOND Sali.[/]")
    console.print("[dim]Keeping the twin current is one of the living Sali's faculties; the daemon "
                  "runs it every 5 minutes (see the twin faculty in `sali daemon`).[/]\n")
    console.print("  Give Sali his permanent life instead:\n"
                  "    [bold]sudo cp systemd/sali.service /etc/systemd/system/[/]\n"
                  "    [bold]sudo systemctl daemon-reload && sudo systemctl enable --now sali[/]\n")
    if _unit_path().exists():
        console.print("[yellow]An old sali-observe unit is still installed. Remove it:[/]\n"
                      "    [bold]sali observe --uninstall[/]")
    raise typer.Exit(1)


def _uninstall_observe_service() -> None:
    import shutil
    import subprocess

    if shutil.which("systemctl") is not None:
        subprocess.run(["systemctl", "--user", "disable", "--now", _SERVICE_NAME], check=False)
    path = _unit_path()
    if path.exists():
        path.unlink()
    console.print("[green]Background observation stopped and removed.[/]")


@app.command()
def observe(
    interval: int = typer.Option(180, help="Seconds between observation cycles."),
    install: bool = typer.Option(False, "--install", help="Run it in the background via systemd (user service)."),
    uninstall: bool = typer.Option(False, "--uninstall", help="Stop and remove the background service."),
) -> None:
    """Watch the machine: re-observe on a loop and surface meaningful changes (§16). Ctrl-C stops.

    Cheap by design — routine cycles change nothing and cost no reasoning; only genuine
    structural changes (a package installed, a project appeared, a service gone) are surfaced.
    Use --install to run it continuously in the background as a systemd user service.
    """
    if uninstall:
        _uninstall_observe_service()
        return
    if install:
        _install_observe_service(interval)
        return
    settings = load_settings()
    configure_logging("WARNING")
    # Desktop observation is a faculty of the living Sali (the daemon runs the twin loop already).
    _refuse_if_mind_alive("observe", suggest="sali twin  # read what he already knows")
    asyncio.run(_observe(settings, interval))


# How often (in observe cycles) the background service also consolidates learning. Learning is
# heavier (a model call), so it runs far less often than observation.
_LEARN_EVERY = 10
# How often the background service refreshes its Tool-Intelligence picture (discover tools, map
# capabilities, classify authority). Deterministic + diff-based, so cheap; also run once at startup
# (cycle 1) so a fresh machine is inventoried promptly, then periodically.
_TOOL_INTEL_EVERY = 20
# How often the daemon researches a few pending learning gaps online (§9). Rare + internet-gated +
# budget-bounded, so it never crawls — at the 5-min twin cadence this is a few times a day.
_RESEARCH_EVERY = 30
# How often the daemon infers capabilities for a few still-unmapped discovered tools (§8). Bounded +
# each tool attempted once, so it gradually covers the whole PATH without re-probing.
_CAP_INFER_EVERY = 25


async def _observe(settings: Settings, interval: int) -> None:
    from sali.db.pool import create_pool
    from sali.learning.service import LearningService
    from sali.provider.registry import build_provider
    from sali.twin.daemon import TwinDaemon

    pool = await create_pool(settings)
    daemon = TwinDaemon(
        _twin_service(pool, settings), interval=float(interval),
        exclude_projects=tuple(settings.permissions.fs_deny),
    )
    learning = LearningService(pool, build_provider(settings))

    async def on_change(result: Any) -> None:
        for key in result.added:
            console.print(f"[green]  + {key}[/]")
        for key in result.removed:
            console.print(f"[yellow]  - {key}[/]")

    async def on_tick(cycle: int) -> None:
        if cycle == 1 or cycle % _TOOL_INTEL_EVERY == 0:  # keep the tool picture current (§26/§74)
            from sali.twin import tool_intel

            ti = await tool_intel.run_pass(pool)
            if not ti.skipped and (ti.added or ti.removed):
                console.print(f"[cyan]  tools:[/] +{ti.added} / -{ti.removed} "
                              f"([dim]{ti.discovered} known[/])")
        if cycle % _LEARN_EVERY != 0:
            return
        result = await learning.consolidate()  # §17-19: procedures, failures, episodes
        for proc in result.procedures:
            console.print(f"[magenta]  learned procedure:[/] {proc.name} ([dim]{proc.evidence}×[/])")
        if result.failures_recorded:
            console.print(f"[dim]  noted {result.failures_recorded} past failure(s)[/]")
        if result.episodes_created:
            console.print("[dim]  folded recent activity into an episode[/]")
        if result.tool_experiences:
            console.print(f"[dim]  learned {result.tool_experiences} tool experience(s)[/]")

    every = interval * _LEARN_EVERY
    console.print(f"[dim]watching every {interval}s, learning every ~{every}s — Ctrl-C to stop[/]")
    stop = asyncio.Event()
    try:
        await daemon.run(stop=stop, on_change=on_change, on_tick=on_tick)
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop.set()
    finally:
        console.print(f"[dim]stopped after {daemon.cycles} cycles.[/]")
        await pool.close()


@app.command()
def daemon(
    serve: bool = typer.Option(
        True, help="Also serve the REST+WebSocket API in THIS process (one runtime, one model) so the "
                   "iPhone app and `sali agent` are windows into the same Sali."),
    host: str | None = typer.Option(
        None, help="API bind host. Default reads settings.api.bind_host ('0.0.0.0' → reachable on "
                   "the LAN so the iPhone can find Sali via Bonjour). Pass '127.0.0.1' explicitly "
                   "(or set SALI_API__BIND_HOST=127.0.0.1) to force loopback-only."),
    port: int | None = typer.Option(None, help="API port. Default reads settings.api.bind_port (8080)."),
) -> None:
    """THE Sali. This is the one living organism: one Kernel, one AgentRuntime, one AgentLoop, one
    cognitive coordinator, one inference path.

    His faculties (scheduler, twin, perception, syswatch, proactive, investigate, learning) and his
    windows (REST + WebSocket for the iPhone app and `sali agent`) all live in THIS process and share
    THAT one runtime. Starting a second one fails safely. This is what `sudo systemctl start sali`
    runs.
    """
    settings = load_settings()
    configure_logging("WARNING")
    # Typer flags win over Settings so a one-shot override (`sali daemon --host 127.0.0.1`) still
    # works. Without a flag the Settings TOML/env chain decides — default bind is 0.0.0.0 so LAN
    # discovery is useful out of the box; auth is IP-agnostic so this does not weaken security.
    effective_host = host if host is not None else settings.api.bind_host
    effective_port = port if port is not None else settings.api.bind_port
    asyncio.run(_daemon(settings, serve_api=serve, api_host=effective_host, api_port=effective_port))


@app.command()
def perceive() -> None:
    """Watch the desktop live (files + focused window) and print the observations Sali would record —
    a foreground view of the continuous event engine. Ctrl-C to stop. Never acts, only observes."""
    settings = load_settings()
    configure_logging("ERROR")
    asyncio.run(_perceive(settings))


async def _perceive(settings: Settings) -> None:
    from sali.events.base import Observation
    from sali.events.engine import PerceptionEngine
    from sali.perception.service import build_perception

    perception = build_perception(settings)

    async def snapshot() -> Any:
        return await perception.snapshot(ui=False)

    class _ConsoleSink:
        async def observe(self, obs: Observation) -> None:
            imp = obs.importance
            colour = "red" if imp >= 0.7 else "yellow" if imp >= 0.5 else "dim"
            console.print(f"[{colour}]{imp:.2f}[/] {obs.summary}"
                          + (f" [dim](×{obs.count})[/]" if obs.count > 1 else ""))

    p = settings.perception
    engine = PerceptionEngine(
        sink=_ConsoleSink(), snapshot=snapshot, fs_roots=p.watch_roots,
        window_poll_s=p.window_poll_s, aggregate_window_s=p.aggregate_window_s)
    stop = asyncio.Event()
    console.print(f"[dim]Perceiving {p.watch_roots} + the focused window… Ctrl-C to stop.[/]")
    try:
        await engine.run(stop)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop.set()


async def _daemon(settings: Settings, *, serve_api: bool = True,
                  api_host: str = "127.0.0.1", api_port: int = 8080) -> None:
    from sali.api.ws import manager as ws_manager
    from sali.core.mind import SecondMindError
    from sali.kernel import Kernel
    from sali.learning.service import LearningService
    from sali.scheduler.daemon import SchedulerDaemon
    from sali.scheduler.store import ScheduleStore
    from sali.security.confirm import AutoAllowConfirmer
    from sali.twin.daemon import TwinDaemon

    # Apply any outstanding DB migrations at boot. The daemon path skipped this (only `sali init` and
    # `sali agent` ran it), so new migrations never auto-applied and had to be run by hand. Idempotent +
    # transactional; runs before the mind starts so the schema is always current.
    from sali.db.bootstrap import init_database
    await init_database(settings)

    # Seed the default API login password on the FIRST boot only — NULL-guarded, so a `sali change-password`
    # rotation is never clobbered by a restart. The iPhone app authenticates with this; the owner changes it
    # with `sali change-password`. Best-effort: a seed failure must never stop the daemon from coming up.
    try:
        from sali.api.password import hash_password
        from sali.db.pool import connect as _pw_connect

        _pw_conn = await _pw_connect(settings)
        try:
            _pw_existing = await _pw_conn.fetchval(
                "SELECT auth_password_hash FROM sali.sali_state WHERE id = true")
            if _pw_existing is None:
                await _pw_conn.execute(
                    "UPDATE sali.sali_state SET auth_password_hash = $1, auth_password_set_at = now() "
                    "WHERE id = true", hash_password("masaka"))
                console.print("[yellow]Seeded default API password ('masaka') — change it with "
                              "`sali change-password`.[/]")
        finally:
            await _pw_conn.close()
    except Exception as _pw_exc:  # noqa: BLE001 - never block boot on the password seed
        console.print(f"[yellow]password seed skipped: {_pw_exc}[/]")

    # BECOME the one Sali — machine-wide, before a pool or a model exists. A second daemon (a stray
    # `systemctl start`, a hand-run copy, a half-finished restart) stops here having changed nothing.
    try:
        kernel = await Kernel.become_mind(
            settings, api_host=(api_host if serve_api else None),
            api_port=(api_port if serve_api else None))
    except SecondMindError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(1) from None

    # THE ONE runtime for this machine. The daemon's own autonomous turns go through submit_background();
    # the same single AgentLoop also serves the terminal `sali agent` and the iPhone API below — one mind,
    # one model. ws_broadcaster wires live events to the WebSocket so those windows see what Sali does.
    # AutoAllow: this is the owner's home machine and the owner drives it (Sali runs free); machine safety
    # is enforced by the GPU lease + resource guards, not a y/n confirmer.
    runtime = await kernel.runtime(confirmer=AutoAllowConfirmer(), ws_broadcaster=ws_manager)
    pool = await kernel.pool()
    # §14/§15: the daemon's autonomous turns run in a SEPARATE conversation.
    session = background_session_id()
    # ONE provider for the whole organism — the kernel's. Faculties that need the model borrow this
    # handle rather than calling build_provider() themselves: a second provider object is a second
    # inference path, and inference paths are what "one mind" is about.
    provider = kernel.provider
    # MODEL SWITCHER: restore the owner's chosen chat model so a switch survives restarts. NULL = the
    # configured default. Best-effort — a missing column or row just leaves the default in place.
    import contextlib as _contextlib
    with _contextlib.suppress(Exception):
        async with pool.acquire() as _mc_conn:
            _saved_model = await _mc_conn.fetchval(
                "SELECT active_chat_model FROM sali.sali_state WHERE id = true")
        if _saved_model and hasattr(provider, "set_active_model"):
            provider.set_active_model(_saved_model)
            # Evict anything ollama kept resident from before the restart (e.g. the previous model
            # pinned by keep_alive=24h) so only the restored active model can occupy VRAM. BOUNDED:
            # this is best-effort and runs during boot — a slow/stalled ollama once wedged startup for
            # minutes here (mind_acquired logged, port never bound). ollama.py bounds each call; this
            # outer ceiling guarantees the whole restore can never hold boot hostage. A timeout just
            # skips eviction (the switch path + vram-janitor evict later).
            _evicted = []
            if hasattr(provider, "ensure_only_loaded"):
                with _contextlib.suppress(Exception):
                    _evicted = await asyncio.wait_for(
                        provider.ensure_only_loaded(_saved_model), timeout=30.0)
            from sali.obs.log import get_logger as _get_mlog
            _get_mlog("sali.daemon").info("active_model_restored", model=_saved_model,
                                          evicted=_evicted)

    class _LoopRunner:
        async def run(self, prompt: str) -> Any:
            return await runtime.submit_background(prompt, session_id=session)

    scheduler = SchedulerDaemon(ScheduleStore(pool), _LoopRunner(), pool=pool, poll_s=30.0)
    twin = TwinDaemon(_twin_service(pool, settings, provider), interval=300.0,
                      exclude_projects=tuple(settings.permissions.fs_deny))
    learning = LearningService(pool, provider)
    stop = asyncio.Event()

    def _almir_is_waiting() -> bool:
        """Whether a foreground turn wants the machine. Consolidation, research and capability
        inference all call the model directly on this faculty — they take no execution lease, so
        nothing else stops them, and inference is a single no-priority semaphore. Learning is
        supposed to happen in free time; this is what makes 'free time' mean something."""
        try:
            return bool(runtime.coordinator.foreground_demanded)
        except Exception:  # noqa: BLE001 — never let the guard itself break the faculty
            return False

    async def on_tick(cycle: int) -> None:
        if cycle == 1 or cycle % _TOOL_INTEL_EVERY == 0:  # keep the tool picture current (§26/§74)
            from sali.twin import tool_intel

            await tool_intel.run_pass(pool)   # DB + PATH scan only, no model call — safe to run
        # Everything below calls the model. Check before each one, not just once: consolidate can
        # take minutes, and Almir may well have started typing during it.
        if cycle % _LEARN_EVERY == 0 and not _almir_is_waiting():
            await learning.consolidate()
        if cycle % _RESEARCH_EVERY == 0 and not _almir_is_waiting():
            from sali.learning.research import research_pass

            await research_pass(pool, provider)
        if cycle % _CAP_INFER_EVERY == 0 and not _almir_is_waiting():
            from sali.twin.capabilities import infer_unmapped

            async with pool.acquire() as conn:  # no outer txn — makes model + subprocess calls
                await infer_unmapped(conn, provider)

    # Each faculty runs under a supervisor: one crashing is logged and restarted (with backoff) rather
    # than cancelling its siblings — a hiccup in perception never takes the scheduler down with it.
    faculties: list[tuple[str, Any]] = [
        ("scheduler", lambda: scheduler.run_forever()),
        ("twin", lambda: twin.run(stop=stop, on_tick=on_tick)),
    ]
    engine = _perception_engine(settings, pool)  # continuous desktop perception (§7,11,42); None if off
    if engine is not None:
        faculties.append(("perception", lambda: engine.run(stop)))
    # System-state watcher (§13/§78): new listening ports, failed units, disk pressure → attention.
    # Ports are compared against a PERSISTENT learned baseline (§19), so a change while Sali was off
    # is still recognised as new.
    from sali.events.baseline import Baseline
    from sali.events.proactive import ProactiveLoop
    from sali.events.sink import DbObservationSink
    from sali.events.syswatch import SystemWatch

    syswatch = SystemWatch(DbObservationSink(pool), baseline=Baseline(pool))
    faculties.append(("syswatch", lambda: syswatch.run(stop)))

    # Audit fix: QUEUE_FOR_LATER messages ("after you finish, do X") were durably persisted but never
    # drained. This idle loop claims them once Sali has no active primary and the foreground is free.
    async def _drain_queued_loop() -> None:
        from sali.obs.log import get_logger as _get_dlog

        dlog = _get_dlog("sali.daemon.drain")
        while not stop.is_set():
            try:
                await runtime.drain_queued()
            except Exception as exc:  # noqa: BLE001 - drain must never take a faculty down, but never silent
                dlog.warning("drain_queued_tick_failed", error=str(exc)[:200])
            try:
                await asyncio.wait_for(stop.wait(), timeout=20.0)
            except TimeoutError:
                pass

    # LONG-HORIZON WORK (multi-day coding tasks). Two gaps closed here, both real:
    #   1. `recover_tasks()` existed and was complete, but NOTHING in the daemon ever called it — only
    #      the terminal path did (`sali agent`). So a restart, crash or reboot orphaned an in-progress
    #      task FOREVER; two real tasks sat 'running' with no heartbeat for six hours and nothing
    #      noticed. Recovery now runs once at startup, adopting whatever the last process left behind.
    #   2. `_maybe_resume_primary` is reachable only from `handle_message`, so an unattended task
    #      simply stopped between turns — a 2-day task could only advance when Almir happened to
    #      speak. `continue_primary()` drives it, through the same coordinator and the same single
    #      cognition slot, yielding the moment he does speak.
    # Recovery is startup-ONLY on purpose: `recover_task` increments retry_count, so putting it on a
    # timer would burn a task's retries every couple of minutes and fail it outright. Continuation
    # keeps the heartbeat fresh, which is what stops a live task from ever looking orphaned.
    async def _task_continuation_loop() -> None:
        from sali.obs.log import get_logger  # `log` in _supervise is local to it, not to this scope

        tlog = get_logger("sali.daemon.tasks")
        try:
            # BEFORE recovery, and before the first turn: `note_downtime` reads the last state write,
            # and both of those overwrite it. This is the only moment the gap is still measurable.
            gap = await runtime.note_downtime()
            if gap.get("seconds", 0) > 60:
                tlog.info("daemon_downtime", seconds=round(gap["seconds"]),
                          missed=len(gap.get("missed_schedules") or []))
        except Exception as exc:  # noqa: BLE001 - never fatal; an unknown gap is just an unknown gap
            tlog.warning("daemon_downtime_failed", error=str(exc)[:200])
        try:
            recovered = await runtime.recover_tasks()
            if recovered:
                tlog.info("daemon_recovered_tasks", count=len(recovered))
        except Exception as exc:  # noqa: BLE001 - recovery is best-effort, never fatal
            tlog.warning("daemon_task_recovery_failed", error=str(exc)[:200])
        # Continuation must never become a treadmill. If a task keeps burning turns without completing a
        # STEP, driving it again is just spending GPU (and this host runs a tight power envelope). Track
        # completed-step count per task; after `_STAGNANT_LIMIT` consecutive no-progress turns, stop
        # driving that task and say so once. The task stays 'running' and visible — a human decision,
        # not a silent stall, and any real progress resets the counter immediately.
        _STAGNANT_LIMIT = 12
        last_done: dict[str, int] = {}
        stagnant: dict[str, int] = {}
        while not stop.is_set():
            try:
                primary = await runtime.active_task_snapshot()
                if primary is not None:
                    tid, done = primary
                    if last_done.get(tid) != done:
                        last_done[tid], stagnant[tid] = done, 0
                    if stagnant.get(tid, 0) < _STAGNANT_LIMIT:
                        started = await runtime.continue_primary()
                        if started:
                            stagnant[tid] = stagnant.get(tid, 0) + 1
                            if stagnant[tid] == _STAGNANT_LIMIT:
                                tlog.warning("task_continuation_stalled", task_id=tid,
                                             steps_done=done, turns=_STAGNANT_LIMIT)
            except Exception as exc:  # noqa: BLE001 - bad cycle must not take faculty down, but never silent
                tlog.warning("task_continuation_tick_failed", error=str(exc)[:200])
            try:
                await asyncio.wait_for(stop.wait(), timeout=45.0)
            except TimeoutError:
                pass

    faculties.append(("task-continuation", _task_continuation_loop))

    # VRAM JANITOR. The KV cache grows across a session (measured 90.9% -> 96.7% of a 12 GB card in one
    # afternoon) and nothing ever reclaimed it, so the card crept toward whatever ceiling was set until
    # Sali stalled against its own residency. When Sali has been idle a while, drop the model so the
    # cache resets; the next turn reloads it. Only ever fires with no foreground work, no active task and
    # a free lease, so it can never interrupt Almir or a long-running task.
    async def _vram_janitor_loop() -> None:
        from sali.obs.log import get_logger as _get_vlog

        vlog = _get_vlog("sali.daemon.vram")
        idle_ticks = 0
        while not stop.is_set():
            try:
                if await runtime.release_idle_model():
                    idle_ticks = 0
                else:
                    idle_ticks += 1
            except Exception as exc:  # noqa: BLE001 - housekeeping must not take faculty down, but never silent
                vlog.warning("vram_janitor_tick_failed", error=str(exc)[:200])
            try:
                await asyncio.wait_for(stop.wait(), timeout=600.0)  # 10 min between checks
            except TimeoutError:
                pass

    from sali.obs.log import get_logger as _get_glogger

    _glog = _get_glogger("sali.grounding")

    async def _grounding_loop() -> None:
        """Go and check the beliefs Sali has flagged as unverified.

        `needs_grounding` marks a claim Sali could settle by looking — something installed, a service, a
        path, hardware. Until now it was only ever WRITTEN. Nothing read it, so the flag was an honest
        label on a belief that stayed unverified forever, and the label itself never reached anything that
        could act. Told "I use Neovim" on a host where Neovim is not installed, Sali stored it, flagged it,
        and went on believing it.

        This closes the loop the cheap way: it does not run its own inference. It picks the most important
        unverified claim and hands it to the SAME agent loop as a background turn, which yields to Almir
        the moment he speaks, and lets Sali use its ordinary tools to look. The turn is expected to end in
        `memory_verify` or `memory_forget` — both already exist and already write through the one canonical
        writer (reground / forget), so nothing here is a second path into memory.

        Deliberately slow and one-at-a-time: this is housekeeping competing for a single cognition slot,
        not a batch job. Nothing is more important than Almir's next message.
        """
        while not stop.is_set():
            try:
                if not runtime.is_busy:   # a property, not a method
                    async with pool.acquire() as conn:
                        await conn.execute("SET search_path TO sali, public")
                        row = await conn.fetchrow(
                            "SELECT m.id, m.content, "
                            # HOW MANY NEAR-IDENTICAL NOTES HE HAS ALREADY WRITTEN.
                            #
                            # Almir, watching the runaway: "sali supposed to understand that he did
                            # that multiple times and he can remove it and not to fire tasks." He could
                            # not: sixteen near-duplicate memories each carried evidence_count = 1, so
                            # every one looked brand new and he dutifully checked it again. The prefix
                            # match is deliberately blunt — they differ only in trailing wording, and an
                            # exact content hash, which is all the writer dedupes on, sees them as
                            # entirely distinct rows.
                            "  (SELECT count(*) FROM memory d WHERE d.valid_until IS NULL "
                            "     AND d.layer = m.layer AND d.id <> m.id "
                            "     AND left(d.content, 60) = left(m.content, 60)) AS near_duplicates "
                            "FROM memory m "
                            "WHERE m.needs_grounding AND m.valid_until IS NULL "
                            "  AND m.superseded_by IS NULL "
                            "  AND m.source <> 'external_source'::memory_source "
                            # An ALLOW-list, matching _GROUNDABLE_LAYERS in the writer: only a claim
                            # about current observable state has an answer to "go and look". An
                            # episodic memory is a past event — asking Sali to check one made him
                            # RE-RUN the task it described, which wrote a new experience, which was
                            # flagged, which woke this loop again. A procedure is verified by USE.
                            "  AND m.layer::text IN ('semantic','system_env') "
                            # Tool-usage aggregates (INFERENCE source, functional claim_key
                            # like tool:echo) are computed statistics over history, not
                            # observable claims. Belt-and-suspenders with the writer fix -
                            # a stale row from before the fix must not fire either.
                            "  AND NOT (m.source = 'inference' AND m.claim_key LIKE 'tool:%') "
                            "  AND m.last_verified < now() - interval '10 minutes' "
                            "ORDER BY m.importance DESC, m.last_verified ASC LIMIT 1"
                        )
                    if row is not None:
                        # THE THIRD OPTION. Verify-or-forget assumed the belief was worth having; when
                        # a dozen near-copies exist the honest answer is neither, but that his own
                        # notes have piled up and want tidying. Without this he had two doors, and both
                        # led back to checking the same thing again.
                        crowded = int(row["near_duplicates"] or 0)
                        crowd_note = (
                            f" You have already written {crowded} near-identical notes to this one — "
                            "a sign you have been round this loop before. Prefer memory_forget on the "
                            "redundant copies over checking the same thing again."
                            if crowded >= 2 else ""
                        )
                        from sali.runtime.session import background_session_id as _bg_sid
                        # THE one that produced "All cleared up. I've retired the stale belief —
                        # bash is right here at /usr/bin/bash". Checking his own notes is housekeeping;
                        # it is not something Almir asked for and does not belong in his chat.
                        await runtime.submit_background(
                            "You wrote this down but never checked it: "
                            f'"{row["content"]}".{crowd_note} Look on this machine now — a quick look, '
                            "NOT a task: never call plan_task for your own housekeeping. Then you MUST "
                            "finish by calling memory_verify (if it holds up) or memory_forget (if it "
                            "does not, or if it is a redundant duplicate). Looking without recording "
                            "the verdict leaves the belief exactly as unsure as before and wastes the "
                            "check. Do not answer from memory. Say nothing to Almir; this is your own "
                            "housekeeping.",
                            session_id=_bg_sid(), priority="background",
                        )
                        # BACK OFF WHETHER OR NOT IT SETTLED. Observed on the first real run: Sali did go
                        # and look (list_directory, verified) and then simply answered without calling
                        # memory_verify — so the flag stayed set, last_verified stayed put, and the same
                        # claim would have been re-picked every five minutes forever. Stamping the attempt
                        # turns that into a bounded retry: still flagged, still honest that it is
                        # unverified, but the next attempt is a staleness window away instead of
                        # immediate. Only memory_verify/memory_forget can actually clear the flag.
                        async with pool.acquire() as conn:
                            await conn.execute("SET search_path TO sali, public")
                            await conn.execute(
                                "UPDATE memory SET last_verified = now() WHERE id = $1", row["id"])
            except Exception as exc:  # noqa: BLE001 - never takes the faculty down, but never silent
                # A swallowed exception here is indistinguishable from "nothing to ground", which is
                # exactly the failure mode this faculty exists to fix. It stays non-fatal; it does not
                # stay quiet.
                _glog.warning("grounding_pass_failed", error=str(exc)[:300])
            try:
                await asyncio.wait_for(stop.wait(), timeout=300.0)  # 5 min between checks
            except TimeoutError:
                pass

    faculties.append(("grounding", _grounding_loop))
    faculties.append(("vram-janitor", _vram_janitor_loop))
    faculties.append(("inbox-drain", _drain_queued_loop))
    # §14 push bus: consumers are woken the instant an event commits (LISTEN), keeping their interval
    # only as a fallback heartbeat. Subscribe before start so the wakers are registered.
    from sali.events.bus import EventBus
    from sali.events.investigate import InvestigateLoop

    bus = EventBus(pool)
    # runtime= is what lets it speak into Almir's CHAT instead of a libnotify popup at
    # DISPLAY=:0, which on this headless box reached nobody.
    # Sali writes his own unprompted messages from here on. The DECISION to speak stays in the
    # deterministic gates; this only hands the phrasing to the model, and declines whenever Almir is
    # being served. Unbound (tests, one-shots) every site falls back to its old fixed wording.
    from sali.cognitive import voice as _voice
    _voice.bind(provider=provider, coordinator=runtime.coordinator, pool=pool)

    proactive = ProactiveLoop(pool, runtime=runtime)   # §16: surface what Sali notices
    proactive_wake = bus.subscribe()
    faculties.append(("proactive", lambda: proactive.run(stop, proactive_wake)))
    # Attention → wake (§12/§17): an 'investigate' verdict drives one autonomous investigate-and-inform
    # turn through the SAME agent loop (unattended AutoDeny confirmer denies anything destructive).
    investigate = InvestigateLoop(pool, _LoopRunner())
    investigate_wake = bus.subscribe()
    faculties.append(("investigate", lambda: investigate.run(stop, investigate_wake)))

    # Cognitive-cycle driver: the missing periodic caller for InitiativeEngine.generate_candidates
    # (audit found it dormant — all the components existed but nothing called them). Runs every
    # 120s with a resource + next-wake gate. Does NOT decide to act — that's the coordinator's
    # job; this just keeps the initiative queue fresh and observable. See src/sali/cognitive/.
    from sali.cognitive.initiative_driver import InitiativeDriver

    initiative_driver = InitiativeDriver(pool, publisher=runtime._publisher,
                                          resource_monitor=getattr(runtime, "_resources", None),
                                          runtime=runtime)
    initiative_wake = bus.subscribe()
    faculties.append(("initiative-driver",
                      lambda: initiative_driver.run(stop, initiative_wake)))
    # Publish the driver to the app state so /cognitive-metrics can read status().
    kernel._initiative_driver = initiative_driver  # type: ignore[attr-defined]

    # Knowledge maturity (§27): drive the dormant DAILY CONSOLIDATION — it promotes learning candidates,
    # archives stale experiments, and reconciles the day, the maturity layer nothing else ran (0 callers
    # before this; consolidation_run had 0 rows ever). Idempotent via the consolidation_run(ran_on)
    # UNIQUE, so the hourly re-check is a clean skip until the UTC day rolls; it takes no execution lease
    # and makes only background model calls, so it never contends with Almir's turns. Borrows the ONE
    # provider handle (never a second inference path). Proven idempotent on sali_admtest before wiring.
    from sali.learning.daily import DailyConsolidation
    from sali.learning.service import LearningService

    _daily = DailyConsolidation(pool, provider, runtime._publisher,
                                learning_service=LearningService(pool, provider))
    _dclog = _get_glogger("sali.consolidation")

    async def _consolidation_loop() -> None:
        while not stop.is_set():
            with _contextlib.suppress(Exception):  # one bad cycle must never kill the faculty
                summaries = await _daily.catch_up(max_cycles=3)  # a re-fire before the day rolls is a no-op
                ran = [str(s.ran_on) for s in (summaries or []) if not s.skipped]
                if ran:
                    _dclog.info("daily_consolidation_ran", days=ran)
            try:
                await asyncio.wait_for(stop.wait(), timeout=3600.0)  # re-check hourly
            except TimeoutError:
                pass

    faculties.append(("consolidation", _consolidation_loop))

    # ONE mind, many windows: host the REST + WebSocket API IN THIS PROCESS, sharing the single runtime
    # above. The iPhone app and the terminal `sali agent` connect here — they are windows into the same
    # Sali, never a second model. (Almir's rule: Sali is one human doing one thing at a time.)
    if serve_api:
        import uvicorn

        from sali.api.app import create_app
        api_app = _api_app(kernel, runtime, pool, create_app)  # injected runtime → never a second loop
        # Publish the bound port to app.state so the lifespan's mDNS advertiser knows what to
        # announce; the bind_host is what uvicorn listens on but the ADVERTISED port must match.
        api_app.state.api_port = api_port
        api_app.state.api_bind_host = api_host
        api_server = uvicorn.Server(uvicorn.Config(
            api_app, host=api_host, port=api_port, log_level="warning"))
        faculties.append(("api", lambda: api_server.serve()))
        kernel.publish_endpoint(api_host, api_port)  # windows discover him here, from the mind lock

    await bus.start()
    console.print("[dim]Sali is up — observing, learning, perceiving, watching schedules"
                  + (f", and serving {api_host}:{api_port} (iPhone + terminal share this one mind)." if serve_api else ".") + "[/]")
    try:
        await asyncio.gather(*(_supervise(name, make, stop) for name, make in faculties))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.stop()
        stop.set()
        await bus.stop()
        await runtime.aclose()
        await kernel.close()


async def _supervise(name: str, make_coro: Any, stop: asyncio.Event) -> None:
    """Run a daemon faculty until it finishes cleanly (stop set); on an unexpected crash, log it and
    restart after a short backoff — so no single faculty can bring the whole daemon down."""
    import contextlib

    from sali.obs.log import get_logger

    log = get_logger("sali.daemon")
    while not stop.is_set():
        try:
            await make_coro()
            return  # exited cleanly — stop was set
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate the faculty; the daemon lives on
            log.error("daemon_faculty_crashed", faculty=name, error=str(exc)[:300])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5.0)  # backoff, but wake immediately on stop


def _perception_engine(settings: Settings, pool: Any = None) -> Any:
    """Build the continuous event engine from settings, or None if watching is disabled. The window
    source is the Phase-4 perception snapshot; the fs source watches the configured roots; surfaced
    observations are PERSISTED (via DbObservationSink) so the agent loop becomes aware of them."""
    if not settings.perception.watch_enabled:
        return None
    from sali.events.engine import PerceptionEngine
    from sali.events.sink import DbObservationSink
    from sali.perception.service import build_perception

    perception = build_perception(settings)

    # The window read costs ~4ms and runs every 3s; the accessibility tree costs ~86ms and only
    # means something different when the focused window CHANGES. So: cheap read always, deep read on
    # a switch. Before this, every production caller passed ui=False and the a11y layer — 25 live
    # applications with fully walkable trees — was never once consulted by anything continuous.
    _last_focus: dict[str, tuple[str, str]] = {"key": ("", "")}

    async def snapshot() -> dict[str, Any]:
        snap = await perception.snapshot(ui=False)
        win = (snap or {}).get("window") or {}
        key = (str(win.get("app") or ""), str(win.get("title") or ""))
        if key[0] and key != _last_focus["key"]:
            _last_focus["key"] = key
            try:
                snap = await perception.snapshot(ui=True)
            except Exception:  # noqa: BLE001 - a slow/absent a11y bus must never stall perception
                pass
        return snap

    p = settings.perception
    sink = DbObservationSink(pool) if pool is not None else None  # None → engine's default LoggingSink
    return PerceptionEngine(
        sink=sink, snapshot=snapshot, fs_roots=p.watch_roots, window_poll_s=p.window_poll_s,
        aggregate_window_s=p.aggregate_window_s, buffer_size=p.observation_buffer)


@app.command()
def service(
    uninstall: bool = typer.Option(False, "--uninstall", help="Print how to remove the service."),
) -> None:
    """Print the exact commands to run Sali as a systemd SYSTEM service — so you can
    `sudo systemctl start sali` (and it's enabled at boot). Needs sudo, which Sali can't do itself."""
    import os
    import sys

    sali_bin = Path(sys.executable).parent / "sali"
    workdir = Path(__file__).resolve().parents[3]
    uid = os.getuid()
    if uninstall:
        console.print("[bold]Remove the Sali service:[/]")
        console.print("  sudo systemctl disable --now sali\n  sudo rm /etc/systemd/system/sali.service"
                      "\n  sudo systemctl daemon-reload\n  sudo rm -f /usr/local/bin/sali")
        return
    unit = f"""[Unit]
Description=Sali — personal AI agent (background presence)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User={os.getenv("USER", "almir")}
WorkingDirectory={workdir}
Environment=DISPLAY=:0
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus
ExecStart={sali_bin} daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    # A STABLE path (not /tmp, which gets cleared) so the install command always finds the unit.
    unit_path = Path.home() / ".config" / "sali" / "sali.service"
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit, encoding="utf-8")

    # Put `sali` on PATH WITHOUT sudo — ~/.local/bin is already on PATH. Only the service needs sudo.
    link = Path.home() / ".local" / "bin" / "sali"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(sali_bin)
    console.print(f"[green]✓ `sali` is on your PATH[/] ({link} → the venv). Open a new terminal "
                  "(or run `hash -r`) and `sali agent` works anywhere.\n")

    console.print("[bold]To run Sali as a system service, paste this ONE line (it needs sudo):[/]\n")
    console.print(f"  sudo cp {unit_path} /etc/systemd/system/sali.service && "
                  "sudo systemctl daemon-reload && sudo systemctl enable --now sali\n")
    console.print("[dim]Then control it with: sudo systemctl start/stop sali  ·  status: "
                  "systemctl status sali  ·  logs: journalctl -u sali -f[/]")
    console.print("[dim]Retire the old watcher if present: "
                  "systemctl --user disable --now sali-observe.service[/]")


@app.command()
def chat(think: bool = typer.Option(False, "--think", help="Show the model's reasoning.")) -> None:
    """Direct model chat — a preview with no memory or tools (use `sali agent` for the full loop)."""
    settings = load_settings()
    configure_logging("WARNING")
    # A raw model REPL beside a living Sali is the purest second mind: same model, no memory,
    # no journal, no arbitration.
    _refuse_if_mind_alive("chat")
    asyncio.run(_chat(settings, think))


async def _chat(settings: Settings, think: bool) -> None:
    from rich.markdown import Markdown

    from sali.provider.base import ChatMessage
    from sali.provider.registry import build_provider

    provider = build_provider(settings)
    console.print("[dim]Sali (preview) — Ctrl-D to exit. Direct model chat; no memory or tools.[/]")
    history = [ChatMessage(role="system", content=IDENTITY)]
    while True:
        try:
            user = console.input("[bold cyan]sali ›[/] ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not user.strip():
            continue
        history.append(ChatMessage(role="user", content=user))
        result = await provider.chat(history, think=think)
        if think and result.thinking:
            console.print(f"[dim]{result.thinking.strip()}[/]")
        console.print(Markdown(result.content.strip()))
        history.append(ChatMessage(role="assistant", content=result.content))
