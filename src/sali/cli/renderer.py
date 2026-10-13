"""Sali's advanced terminal renderer.

A rich, modern terminal experience for chatting with Sali. Features:
- Animated header with system status
- Color-coded tool execution with timing
- Streaming markdown with syntax highlighting
- Task progress visualization
- Memory/retrieval indicators
- Session context bar
- Thinking/reasoning display
- Error formatting with context
"""

from __future__ import annotations

import time
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

# ── Color Palette ──────────────────────────────────────────────────────────────
# A cohesive dark-theme palette. Every color has a semantic meaning.

class C:
    """Color constants for the terminal UI."""
    # Prompt
    USER_PROMPT = "bold #00d7ff"       # bright cyan — user input
    SALI_PREFIX = "bold #5faf5f"       # green — Sali's name
    SALI_TEXT = "#d4d4d4"              # light gray — Sali's words

    # Status
    THINKING = "#875faf"               # purple — thinking/reasoning
    WORKING = "#d7af5f"                # amber — working on something
    SUCCESS = "#5faf5f"                # green — success
    ERROR = "#d75f5f"                  # red — error
    WARNING = "#d7af5f"                # amber — warning
    DIM = "#666666"                    # dim gray — secondary info
    MUTED = "#808080"                  # muted — less important

    # Tools
    TOOL_NAME = "#5fafd7"              # cyan — tool name
    TOOL_ARG = "#878787"               # gray — tool arguments
    TOOL_OK = "#5faf5f"                # green — tool success
    TOOL_FAIL = "#d75f5f"              # red — tool failure
    TOOL_RUNNING = "#d7af5f"           # amber — tool running

    # Memory
    MEMORY_HIT = "#5f87d7"             # blue — memory recalled
    MEMORY_SOURCE = "#878787"          # gray — memory source

    # Task
    TASK_STEP_DONE = "#5faf5f"         # green — step done
    TASK_STEP_ACTIVE = "#d7af5f"       # amber — step active
    TASK_STEP_PENDING = "#666666"      # dim — step pending
    TASK_STEP_FAILED = "#d75f5f"       # red — step failed

    # Session
    SESSION_ID = "#5f5f87"             # dark blue — session id
    MODEL_NAME = "#875faf"             # purple — model name


# ── Symbols ────────────────────────────────────────────────────────────────────

class S:
    """Unicode symbols for the terminal UI."""
    PROMPT_USER = "you"                # user input prompt
    PROMPT_SALI = "sali"               # Sali's prefix
    THINKING = "⟡"                     # thinking indicator
    TOOL_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille spinner frames
    TOOL_OK = "✓"                      # tool success
    TOOL_FAIL = "✗"                    # tool failure
    TOOL_RUNNING = "⟳"                 # tool running
    MEMORY = "◈"                       # memory recalled
    STEP_DONE = "●"                    # step completed
    STEP_ACTIVE = "◐"                  # step in progress
    STEP_PENDING = "○"                 # step pending
    STEP_FAILED = "◉"                  # step failed
    DIVIDER = "─"                      # horizontal divider
    BULLET = "•"                       # bullet point
    ARROW = "→"                        # arrow
    CHECK = "✓"                        # checkmark
    CROSS = "✗"                        # cross


# ── Terminal Renderer ──────────────────────────────────────────────────────────

class TerminalRenderer:
    """Advanced terminal renderer for Sali's agent loop."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._turn_start: float = 0
        self._tool_count: int = 0
        self._token_count: int = 0
        self._current_tool: str | None = None
        self._tool_start: float = 0
        self._session_id: str | None = None
        self._model_name: str | None = None

    def print_header(self, version: str, model: str, session_id: str) -> None:
        """Print the Sali header when starting a session."""
        self._session_id = session_id[:12]
        self._model_name = model

        self.console.print()
        self.console.print(
            f"  [{C.SALI_PREFIX}]{S.PROMPT_SALI} Sali[/] "
            f"[{C.DIM}]v{version}[/]"
        )
        self.console.print(
            f"  [{C.DIM}]model[/] [{C.MODEL_NAME}]{model}[/]  "
            f"[{C.DIM}]session[/] [{C.SESSION_ID}]{session_id[:8]}[/]"
        )
        self.console.print()

    def print_session_bar(self, task_info: str | None = None,
                          memory_count: int | None = None) -> None:
        """Print a context bar showing current session state."""
        parts: list[str] = []
        if task_info:
            parts.append(f"[{C.TASK_STEP_ACTIVE}]task: {task_info}[/]")
        if memory_count is not None:
            parts.append(f"[{C.MEMORY_HIT}]{S.MEMORY} {memory_count} memories[/]")
        if parts:
            self.console.print(f"  [{C.DIM}]{S.BULLET}[/] {' '.join(parts)}")

    def start_turn(self) -> None:
        """Mark the start of a new turn."""
        self._turn_start = time.monotonic()
        self._tool_count = 0
        self._token_count = 0

    def render_user_input(self, text: str) -> None:
        """Render the user's input."""
        self.console.print()
        self.console.print(Text.assemble(
            (f"  {S.PROMPT_USER} ", C.USER_PROMPT),
            (text, C.SALI_TEXT),
        ))
        self.console.print()

    def create_streaming_live(self) -> tuple[Live, StreamingState]:
        """Create a Live display for streaming Sali's response."""
        state = StreamingState()
        # TRANSIENT: the live area is PROGRESS — spinner, elapsed count, text as it arrives — and it
        # is erased when the turn ends. `StreamingState.commit` then writes the finished answer once,
        # as permanent scrollback. Without this the last live frame stays on screen AND commit prints
        # the same answer underneath it, so every reply appeared twice.
        live = Live(
            self._render_streaming_frame(state),
            console=self.console,
            refresh_per_second=15,
            transient=True,
        )
        return live, state

    def _render_streaming_frame(self, state: StreamingState) -> Group:
        """Render the current streaming frame."""
        parts: list[Any] = []

        # Streaming text
        if state.text_buffer:
            parts.append(Text.assemble(
                (f"  {S.PROMPT_SALI} ", C.SALI_PREFIX),
            ))
            parts.append(Markdown(state.text_buffer.rstrip()))

        # Thinking indicator
        if state.thinking and not state.text_buffer:
            thinking_text = state.thinking[:200] + ("…" if len(state.thinking) > 200 else "")
            parts.append(Text.assemble(
                (f"  {S.THINKING} ", C.THINKING),
                (f"thinking: {thinking_text}", f"dim {C.THINKING}"),
            ))

        # Activity spinner. The Spinner object goes into the render tree ITSELF — `str(spinner)` is a
        # static repr, so the old code drew one motionless frame and never animated. On a local model
        # the wait before the first token is ~35s, and a frozen glyph for that long reads as a hang.
        if state.activity:
            label = state.activity
            if state.started_at:
                label = f"{label}  {time.monotonic() - state.started_at:.0f}s"
            if state.tokens:
                label = f"{label}  {state.tokens:,} tok"
                if state.tokens_per_second >= 1:
                    label = f"{label}  {state.tokens_per_second:.0f}/s"
            parts.append(Padding(Spinner("dots", text=Text(label, style=C.DIM), style=C.WORKING),
                                 (0, 0, 0, 2)))

        return Group(*parts)

    def render_tool_start(self, tool_name: str, args: dict[str, Any] | None = None) -> None:
        """Render the start of a tool execution."""
        self._current_tool = tool_name
        self._tool_start = time.monotonic()
        self._tool_count += 1

        # Show the most relevant argument value cleanly
        arg_str = ""
        if args:
            for key in ("command", "path", "query", "url", "objective"):
                val = args.get(key)
                if val is not None:
                    s = str(val)
                    if len(s) > 70:
                        s = s[:67] + "…"
                    arg_str = f" {s}"
                    break

        self.console.print(Text.assemble(
            (f"  {S.TOOL_RUNNING} ", C.TOOL_RUNNING),
            (tool_name, C.TOOL_NAME),
            (arg_str, C.TOOL_ARG),
        ))

    def render_tool_done(self, tool_name: str, ok: bool, summary: str,
                         duration_ms: int | None = None) -> None:
        """Render the completion of a tool execution."""
        mark = S.TOOL_OK if ok else S.TOOL_FAIL
        color = C.TOOL_OK if ok else C.TOOL_FAIL
        elapsed = ""
        if duration_ms is not None:
            elapsed = (f" {duration_ms / 1000:.1f}s" if duration_ms > 1000
                       else f" {duration_ms}ms")

        # Truncate summary for display
        display_summary = summary or tool_name
        if len(display_summary) > 80:
            display_summary = display_summary[:77] + "…"

        self.console.print(Text.assemble(
            (f"  {mark} ", color),
            (display_summary, C.DIM),
            (elapsed, C.DIM),
        ))
        self._current_tool = None

    def render_memory_recall(self, count: int, query: str | None = None) -> None:
        """Render a memory recall indicator."""
        q = f' for "{query[:40]}"' if query else ""
        self.console.print(Text.assemble(
            (f"  {S.MEMORY} ", C.MEMORY_HIT),
            (f"recalled {count} memories{q}", C.DIM),
        ))

    def render_task_progress(self, steps: list[dict[str, Any]]) -> None:
        """Render task step progress."""
        if not steps:
            return
        parts: list[tuple[str, str]] = []
        for step in steps:
            status = step.get("status", "pending")
            if status == "done":
                parts.append((S.STEP_DONE, C.TASK_STEP_DONE))
            elif status in ("running", "active"):
                parts.append((S.STEP_ACTIVE, C.TASK_STEP_ACTIVE))
            elif status == "failed":
                parts.append((S.STEP_FAILED, C.TASK_STEP_FAILED))
            else:
                parts.append((S.STEP_PENDING, C.TASK_STEP_PENDING))
        progress = " ".join(f"[{color}]{sym}[/]" for sym, color in parts)
        self.console.print(f"  [{C.DIM}]steps:[/] {progress}")

    def render_error(self, error: str, context: str | None = None) -> None:
        """Render an error message."""
        panel = Panel(
            error,
            title=f"[{C.ERROR}]Error[/]",
            border_style=C.ERROR,
            padding=(0, 1),
        )
        self.console.print(panel)
        if context:
            self.console.print(f"  [{C.DIM}]context: {context}[/]")

    def render_turn_summary(self, tool_calls: int, duration_s: float,
                            tokens: int = 0) -> None:
        """Render a subtle turn summary after Sali finishes.

        `tokens` is the model's own eval_count for the turn, so the line says what the turn actually
        cost rather than only how long it took — a slow turn that generated 900 tokens and a slow
        turn that generated 40 look identical on a clock alone.
        """
        elapsed = f"{duration_s:.1f}s" if duration_s >= 1 else f"{duration_s * 1000:.0f}ms"
        parts = []
        if tool_calls > 0:
            parts.append(f"{tool_calls} tool{'s' if tool_calls != 1 else ''}")
        parts.append(elapsed)
        if tokens > 0:
            parts.append(f"{tokens:,} tok")
            if duration_s > 0:
                parts.append(f"{tokens / duration_s:.0f}/s")
        self.console.print(f"  [{'dim'}]{' · '.join(parts)}[/]")

    def render_recovery_info(self, recovered: list[dict[str, Any]]) -> None:
        """Render recovery information on startup."""
        if not recovered:
            return
        self.console.print(f"\n  [{C.WARNING}]Recovering interrupted tasks:[/]")
        for r in recovered:
            self.console.print(
                f"    {S.ARROW} [{C.DIM}]{r.get('task_id', '?')[:8]}[/] "
                f"{r.get('objective', '?')[:60]}"
            )

    def print_divider(self) -> None:
        """Print a subtle horizontal divider."""
        width = min(self.console.width, 80)
        self.console.print(f"  [{C.DIM}]{S.DIVIDER * (width - 4)}[/]")


class StreamingState:
    """Mutable state for the streaming display."""

    def __init__(self) -> None:
        self.text_buffer: str = ""
        self.thinking: str = ""
        self.activity: str | None = "connecting…"
        self.committed_lines: list[str] = []
        # When the turn started, so the spinner can count. A 35-second wait with no number on it is
        # indistinguishable from a hang.
        self.started_at: float | None = None
        # Tokens the model has actually produced this turn, and the rate (agent.generating). The
        # clock alone cannot tell a long answer from a stall; this can, and it starts moving while
        # Sali is still thinking rather than waiting for the first word of prose.
        self.tokens: int = 0
        self.tokens_per_second: float = 0.0

    def feed_token(self, text: str) -> None:
        """Add a token to the streaming buffer."""
        self.text_buffer += text
        self.activity = None

    def set_thinking(self, text: str) -> None:
        """Set the thinking text."""
        self.thinking += text
        if not self.text_buffer:
            self.activity = None

    def set_activity(self, text: str) -> None:
        """Set the activity indicator."""
        self.activity = text

    def commit(self, console: Console) -> None:
        """Commit the current buffer to the permanent transcript."""
        if self.text_buffer.strip():
            console.print(Text.assemble(
                (f"  {S.PROMPT_SALI} ", C.SALI_PREFIX),
            ))
            console.print(Markdown(self.text_buffer.rstrip()))
            self.committed_lines.append(self.text_buffer)
        self.text_buffer = ""
        self.thinking = ""


def _fmt_args(args: dict[str, Any] | None) -> str:
    """Format tool arguments for the activity spinner — show the most relevant arg."""
    if not args:
        return ""
    for key in ("command", "path", "query", "url", "step", "status", "objective"):
        val = args.get(key)
        if val is not None:
            s = str(val)
            if len(s) > 60:
                s = s[:57] + "…"
            return s
    return ""
