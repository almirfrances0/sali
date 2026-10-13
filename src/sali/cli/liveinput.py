"""Realtime keystroke awareness for the terminal.

Ordinary REPLs only see your line once you press Enter. This one watches every keystroke:
prompt_toolkit reports each edit, and when Almir pauses for a beat, Sali forms a *quiet hunch*
about what he's typing — retrieval only, no model, no tokens — and shows it under the input
line. So you can watch Sali notice, in realtime, before you've even finished the thought.
Submitting the line hands control straight back to the normal streaming turn.

This lives in the top ``cli`` layer and is entirely optional: if prompt_toolkit isn't present
the REPL falls back to a plain blocking prompt (see ``cli/main.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory


class SenseInput:
    """A keystroke-aware line reader that shows Sali's live hunch in the bottom toolbar."""

    def __init__(
        self, sense: Callable[[str], Awaitable[str]], *, delay: float = 0.5, min_chars: int = 4
    ) -> None:
        self._sense = sense
        self._delay = delay
        self._min_chars = min_chars
        self._hunch = ""
        self._task: asyncio.Task[None] | None = None
        # Persistent command history — up-arrow recalls previous inputs across sessions.
        from pathlib import Path
        history_dir = Path.home() / ".local" / "share" / "sali"
        history_dir.mkdir(parents=True, exist_ok=True)
        self._session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_dir / "history")),
        )
        self._session.default_buffer.on_text_changed += self._on_change

    def _on_change(self, _buffer: Any) -> None:
        text = self._session.default_buffer.text
        if self._task is not None and not self._task.done():
            self._task.cancel()  # debounce: only the last pause fires
        if len(text.strip()) < self._min_chars:
            self._set_hunch("")
            return
        self._task = asyncio.ensure_future(self._sense_after_pause(text))

    async def _sense_after_pause(self, text: str) -> None:
        try:
            await asyncio.sleep(self._delay)  # wait for a lull in typing
            hunch = await self._sense(text)
            if text == self._session.default_buffer.text:  # ignore if he kept typing
                self._set_hunch(hunch)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - a hunch is decoration, never fatal
            pass

    def _set_hunch(self, hunch: str) -> None:
        if hunch == self._hunch:
            return
        self._hunch = hunch
        with contextlib.suppress(Exception):  # no live app yet (called before prompt starts)
            self._session.app.invalidate()  # redraw the toolbar now

    def _toolbar(self) -> HTML:
        if not self._hunch:
            return HTML("<style fg='#666666'>sali is listening…</style>")
        return HTML(
            f"<style fg='#5fafaf'>sali senses</style>  "
            f"<style fg='#999999'>{escape(self._hunch)}</style>"
        )

    async def prompt(self, message: str = "you › ") -> str:
        """Read one line, live-sensing as it's typed. Raises EOFError/KeyboardInterrupt like
        the standard prompt so the REPL's existing exit handling still works."""
        self._hunch = ""
        text = await self._session.prompt_async(
            HTML(f"<b><style fg='#00afff'>{escape(message)}</style></b>"),
            bottom_toolbar=self._toolbar,
        )
        if self._task is not None and not self._task.done():
            self._task.cancel()  # drop a pending hunch from the final keystrokes
        return text
