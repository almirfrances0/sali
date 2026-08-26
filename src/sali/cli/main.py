"""The `sali` command-line interface.

Phase 0 ships ``version``, ``init`` (apply migrations), and ``doctor`` (health check).
``chat`` is a minimal Phase-1 preview REPL — it is NOT yet the full agent loop, and it
persists nothing.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from sali import __version__
from sali.config.settings import Settings, load_settings
from sali.context.engine import IDENTITY
from sali.obs.log import configure_logging

app = typer.Typer(add_completion=False, help="Sali — a local-first personal AI agent.")
console = Console()


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
    return applied


async def _seed_core(settings: Settings) -> None:
    """Seed Sali's identity + root graph (idempotent). Identity is owned by Sali, not the
    model (spec §27), so it lives in the datastore, not a prompt constant."""
    from sali.core.enums import MemoryLayer, MemorySource
    from sali.db.pool import connect
    from sali.graph import writer as graph_writer
    from sali.memory import writer as memory_writer

    conn = await connect(settings)
    try:
        almir = await graph_writer.ensure_node(
            conn, node_type="person", name="Almir", canonical_key="person:almir",
            source=MemorySource.USER_EXPLICIT,
        )
        sali = await graph_writer.ensure_node(
            conn, node_type="agent", name="Sali", canonical_key="agent:sali",
            source=MemorySource.USER_EXPLICIT,
        )
        await graph_writer.relate(
            conn, src_id=almir.id, dst_id=sali.id, rel_type="uses",
            source=MemorySource.USER_EXPLICIT,
        )
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

    console.print(table)


@app.command()
def agent(
    message: str | None = typer.Argument(None, help="A one-shot question; omit for a REPL."),
) -> None:
    """Run the full journaled agent loop (memory → plan → verified tools → respond)."""
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_agent(settings, message))


async def _agent(settings: Settings, message: str | None) -> None:
    from sali.core.ids import new_id
    from sali.kernel import Kernel
    from sali.security.confirm import TerminalConfirmer

    kernel = Kernel.create(settings)
    loop = await kernel.agent_loop(confirmer=TerminalConfirmer())
    session_id = new_id()  # one session across the REPL: a continuous, remembered conversation
    try:
        if message:
            result = await loop.run(message, session_id=session_id)
            console.print(result.text)
            return
        console.print("[dim]Sali agent — Ctrl-D to exit. Every turn is journaled and remembered.[/]")
        while True:
            try:
                text = console.input("[bold cyan]sali ›[/] ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                return
            if not text.strip():
                continue
            result = await loop.run(text, session_id=session_id)
            console.print(result.text)
    finally:
        await kernel.close()


@app.command()
def recover() -> None:
    """Resolve any agent runs interrupted by a crash (surfaces them; never silent-retries)."""
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_recover(settings))


async def _recover(settings: Settings) -> None:
    from sali.kernel import Kernel
    from sali.security.confirm import AutoDenyConfirmer

    kernel = Kernel.create(settings)
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
def chat(think: bool = typer.Option(False, "--think", help="Show the model's reasoning.")) -> None:
    """Direct model chat — a preview with no memory or tools (use `sali agent` for the full loop)."""
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_chat(settings, think))


async def _chat(settings: Settings, think: bool) -> None:
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
        console.print(result.content.strip())
        history.append(ChatMessage(role="assistant", content=result.content))
