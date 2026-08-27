"""The `sali` command-line interface.

Phase 0 ships ``version``, ``init`` (apply migrations), and ``doctor`` (health check).
``chat`` is a minimal Phase-1 preview REPL — it is NOT yet the full agent loop, and it
persists nothing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from sali import __version__
from sali.config.settings import Settings, load_settings
from sali.context.engine import IDENTITY
from sali.obs.log import configure_logging
from sali.runtime.session import persistent_session_id

app = typer.Typer(add_completion=False, help="Sali — a local-first personal AI agent.")
console = Console()

secrets_cli = typer.Typer(help="Manage Sali's secrets — never stored in the database.")
app.add_typer(secrets_cli, name="secrets")

memory_cli = typer.Typer(help="Inspect Sali's memory.")
app.add_typer(memory_cli, name="memory")


@memory_cli.command("eval")
def memory_eval() -> None:
    """Benchmark the memory system (§67): seed a controlled scenario, score retrieval / resolution /
    temporal / provenance / contradiction / scope accuracy. Runs rolled-back — never touches real data."""
    settings = load_settings()
    configure_logging("ERROR")
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


@secrets_cli.command("set")
def secrets_set(ref: str) -> None:
    """Store a secret (e.g. 'mail.personal.password'), ENCRYPTED at rest in ~/.config/sali/vault.json."""
    from sali.config.secrets import SecretStore
    from sali.config.vault import VaultError

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


def _fmt_tool(data: dict[str, Any]) -> str:
    args = data.get("args") or {}
    value = args.get("command") or args.get("path") or args.get("repo")
    if value is None and args:
        value = next(iter(args.values()), "")
    return str(value or "")[:70]


async def _stream_turn(loop: Any, text: str, session: UUID) -> None:
    """Render one turn as a flowing transcript, like the big assistants: Sali's words stream in
    live, each ● action line lands in order beneath the words that led to it, and the final answer
    stays on screen. It is append-only — nothing already shown is ever wiped, so you can read the
    whole turn top to bottom."""
    from rich.console import Group
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.text import Text

    seg = ""  # the words Sali is currently streaming, not yet committed to the transcript
    activity: str | None = "…"
    spinner = Spinner("dots", style="cyan")
    err: Exception | None = None

    def render() -> Group:
        parts: list[Any] = []
        if seg:
            parts.append(Text.assemble(("sali › ", "bold green"), seg))
        if activity is not None:
            spinner.update(text=Text(f" {activity}", style="dim cyan"))
            parts.append(spinner)
        return Group(*parts)

    with Live(render(), console=console, refresh_per_second=12, transient=False) as live:
        # Let a confirmation prompt pause this spinner (so a destructive-op y/N is visible, not a hang).
        confirmer = getattr(loop, "confirmer", None)
        if confirmer is not None and hasattr(confirmer, "attach"):
            confirmer.attach(live)

        def commit() -> None:
            nonlocal seg
            if seg.strip():  # move the current words up into the permanent transcript
                live.console.print(Text.assemble(("sali › ", "bold green"), seg.rstrip()))
            seg = ""

        try:
            async for event in loop.astream(text, session_id=session):
                if event.kind == "token":
                    seg += event.text
                    activity = None  # the words are flowing — no spinner
                elif event.kind == "status":
                    activity = f"{event.text}…"
                elif event.kind == "thinking":
                    if not seg:
                        activity = "thinking…"
                elif event.kind == "tool":
                    if event.data.get("phase") == "start":
                        commit()  # the words that led here → transcript, then the action below them
                        activity = f"{event.data['name']} {_fmt_tool(event.data)}".strip()
                    else:
                        ok = event.data.get("ok", True)
                        summary = str(event.data.get("summary") or event.data.get("name", ""))
                        mark = "[green]●[/]" if ok else "[red]●[/]"
                        live.console.print(f"{mark} [dim]{summary}[/]")
                        activity = "working…"
                elif event.kind == "final":
                    if event.text and not seg.strip():
                        seg = event.text
                    activity = None
                live.update(render())
        except Exception as exc:  # noqa: BLE001 - re-raised after the last frame is drawn
            err = exc
        finally:
            activity = None
            live.update(render())  # last frame: the final answer, no spinner (kept on screen)
    if err is not None:
        raise err


async def _agent(settings: Settings, message: str | None) -> None:
    from sali.kernel import Kernel
    from sali.security.confirm import TerminalConfirmer

    kernel = Kernel.create(settings)
    loop = await kernel.agent_loop(confirmer=TerminalConfirmer())
    session = persistent_session_id()  # always the same continuous conversation
    try:
        if message:
            await _stream_turn(loop, message, session)
            return
        reader = _live_reader(loop)
        watching = " It watches as you type," if reader is not None else ""
        console.print(f"[dim]talking to Sali — Ctrl-D to leave.{watching} and it remembers.[/]")
        while True:
            try:
                text = await reader.prompt("you › ") if reader is not None \
                    else console.input("[bold cyan]you ›[/] ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                return
            if not text.strip():
                continue
            await _stream_turn(loop, text, session)
    finally:
        await loop.aclose()  # shut down Sali's browser if it was launched
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
    service = RetrievalService(pool, provider)
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
def learn() -> None:
    """Consolidate what Sali has done into knowledge — learn repeated procedures, record failures.

    Learning is memory acquisition, not retraining (§17-19). A procedure is only learned once it
    has repeated evidence — never from a single observation.
    """
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_learn(settings))


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
    asyncio.run(_scheduler(settings, interval))


async def _scheduler(settings: Settings, interval: int) -> None:
    from sali.kernel import Kernel
    from sali.scheduler.daemon import SchedulerDaemon
    from sali.scheduler.store import ScheduleStore
    from sali.security.confirm import AutoDenyConfirmer

    kernel = Kernel.create(settings)
    pool = await kernel.pool()
    # Scheduled turns run with no one at the terminal, so a genuinely-destructive step is auto-declined
    # (Sali still runs everything else freely) rather than blocking on a confirm nobody can answer.
    loop = await kernel.agent_loop(confirmer=AutoDenyConfirmer())
    session = persistent_session_id()

    class _LoopRunner:
        async def run(self, prompt: str) -> Any:
            return await loop.run(prompt, session_id=session)

    daemon = SchedulerDaemon(ScheduleStore(pool), _LoopRunner(), pool=pool, poll_s=float(interval))
    console.print(f"[dim]scheduler running (checking every {interval}s) — Ctrl-C to stop[/]")
    task = asyncio.create_task(daemon.run_forever())
    try:
        await task
    except (KeyboardInterrupt, asyncio.CancelledError):
        daemon.stop()
    finally:
        await kernel.close()


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
    asyncio.run(_twin(settings, refresh))


def _twin_service(pool: Any, settings: Settings) -> Any:
    """A TwinService wired with a memory service, so a refresh also records the machine as
    retrievable system-env facts (grounding "what GPU / how much RAM / what's installed")."""
    from sali.memory.service import MemoryService
    from sali.provider.registry import build_provider
    from sali.twin.service import TwinService

    return TwinService(pool, memory=MemoryService(pool, build_provider(settings)))


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
    """Install + enable a systemd *user* service so `sali observe` runs in the background — no
    root (Sali runs as Almir). Survives across sessions once linger is enabled."""
    import shutil
    import subprocess
    import sys

    sali_bin = Path(sys.executable).parent / "sali"
    if not sali_bin.exists():
        found = shutil.which("sali")
        if not found:
            console.print("[red]Can't find the `sali` executable — install with `make install` first.[/]")
            raise typer.Exit(1)
        sali_bin = Path(found)
    workdir = Path.cwd()
    unit = f"""[Unit]
Description=Sali — desktop observation (keeps the digital twin current)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
ExecStart={sali_bin} observe --interval {interval}
WorkingDirectory={workdir}
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
"""
    path = _unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit, encoding="utf-8")
    if shutil.which("systemctl") is None:
        console.print(f"[yellow]Wrote {path}, but systemctl isn't available to enable it.[/]")
        return
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", _SERVICE_NAME], capture_output=True, text=True
    )
    if result.returncode == 0:
        console.print(f"[green]Sali is now watching in the background[/] (every {interval}s).")
        console.print(f"[dim]  unit: {path}[/]")
        console.print("[dim]  logs: journalctl --user -u sali-observe -f[/]")
        console.print("[dim]  keep it running after logout: loginctl enable-linger[/]")
    else:
        console.print(f"[yellow]Wrote the unit but couldn't enable it:[/] {result.stderr.strip()}")


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
    asyncio.run(_observe(settings, interval))


# How often (in observe cycles) the background service also consolidates learning. Learning is
# heavier (a model call), so it runs far less often than observation.
_LEARN_EVERY = 10


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
        if cycle % _LEARN_EVERY != 0:
            return
        result = await learning.consolidate()  # §17-19: procedures, failures, episodes
        for proc in result.procedures:
            console.print(f"[magenta]  learned procedure:[/] {proc.name} ([dim]{proc.evidence}×[/])")
        if result.failures_recorded:
            console.print(f"[dim]  noted {result.failures_recorded} past failure(s)[/]")
        if result.episodes_created:
            console.print("[dim]  folded recent activity into an episode[/]")

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
def daemon() -> None:
    """Sali's background presence: keep the twin current, consolidate learning, and fire due
    schedules (which can proactively notify Almir). This is what the systemd `sali` service runs."""
    settings = load_settings()
    configure_logging("WARNING")
    asyncio.run(_daemon(settings))


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


async def _daemon(settings: Settings) -> None:
    from sali.kernel import Kernel
    from sali.learning.service import LearningService
    from sali.provider.registry import build_provider
    from sali.scheduler.daemon import SchedulerDaemon
    from sali.scheduler.store import ScheduleStore
    from sali.security.confirm import AutoDenyConfirmer
    from sali.twin.daemon import TwinDaemon

    kernel = Kernel.create(settings)
    pool = await kernel.pool()
    # Scheduled turns run unattended → AutoDeny so a destructive step is skipped, not left hanging.
    loop = await kernel.agent_loop(confirmer=AutoDenyConfirmer())
    session = persistent_session_id()

    class _LoopRunner:
        async def run(self, prompt: str) -> Any:
            return await loop.run(prompt, session_id=session)

    scheduler = SchedulerDaemon(ScheduleStore(pool), _LoopRunner(), pool=pool, poll_s=30.0)
    twin = TwinDaemon(_twin_service(pool, settings), interval=300.0,
                      exclude_projects=tuple(settings.permissions.fs_deny))
    learning = LearningService(pool, build_provider(settings))
    stop = asyncio.Event()

    async def on_tick(cycle: int) -> None:
        if cycle % _LEARN_EVERY == 0:
            await learning.consolidate()

    # Each faculty runs under a supervisor: one crashing is logged and restarted (with backoff) rather
    # than cancelling its siblings — a hiccup in perception never takes the scheduler down with it.
    faculties: list[tuple[str, Any]] = [
        ("scheduler", lambda: scheduler.run_forever()),
        ("twin", lambda: twin.run(stop=stop, on_tick=on_tick)),
    ]
    engine = _perception_engine(settings, pool)  # continuous desktop perception (§7,11,42); None if off
    if engine is not None:
        faculties.append(("perception", lambda: engine.run(stop)))

    console.print("[dim]Sali is up — observing, learning, perceiving, and watching its schedules.[/]")
    try:
        await asyncio.gather(*(_supervise(name, make, stop) for name, make in faculties))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.stop()
        stop.set()
        await loop.aclose()
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

    async def snapshot() -> dict[str, Any]:
        return await perception.snapshot(ui=False)

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
def serve(
    socket: str = typer.Option("", help="Unix socket (default ~/.local/share/sali/sali.sock)."),
    host: str = typer.Option("", help="Bind a TCP host instead (e.g. 127.0.0.1) for the app."),
    port: int = typer.Option(8790, help="TCP port, used with --host."),
) -> None:
    """Run Sali's local WebSocket API — streams turns to the app / a realtime UI."""
    import uvicorn

    from sali.api import create_app
    from sali.kernel import Kernel

    settings = load_settings()
    configure_logging(settings.log_level)
    application = create_app(Kernel.create(settings))
    if host:
        console.print(f"[green]Sali API →[/] ws://{host}:{port}/ws")
        uvicorn.run(application, host=host, port=port, log_level="warning")
    else:
        sock = socket or str(Path.home() / ".local" / "share" / "sali" / "sali.sock")
        Path(sock).parent.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]Sali API →[/] unix:{sock} (path /ws)")
        uvicorn.run(application, uds=sock, log_level="warning")


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
