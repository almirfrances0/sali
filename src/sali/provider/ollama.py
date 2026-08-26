"""Ollama-backed provider (the current concrete backend).

Every machine-parsed call sends the full DETERMINISTIC option block explicitly so the
model's poisoned Modelfile defaults never leak in (fix H6). ``num_ctx`` is clamped well
below the model's advertised 262144 — unusable under 12 GB VRAM (fix H1/§M).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ollama import AsyncClient

from sali.config.settings import ModelSettings
from sali.core.errors import ProviderError
from sali.provider.base import ChatChunk, ChatMessage, ChatResult, ToolCall, ToolSpec
from sali.provider.presets import DETERMINISTIC, SamplingPreset


class OllamaProvider:
    def __init__(self, settings: ModelSettings) -> None:
        self.s = settings
        # Generous timeout so a long single-shot generation (a full page, slowly) never gets
        # cut off mid-stream on modest hardware.
        self._client = AsyncClient(host=settings.host, timeout=settings.request_timeout_s)

    def _build(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
        options: dict[str, Any] | None,
        preset: SamplingPreset,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, dict[str, Any]]:
        opts: dict[str, Any] = preset.to_options()
        opts["num_ctx"] = min(self.s.ctx_default, self.s.ctx_max)
        if options:
            opts.update(options)

        payload: list[dict[str, Any]] = []
        for m in messages:
            entry: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.name:
                entry["tool_name"] = m.name
            if m.tool_calls:
                entry["tool_calls"] = [
                    {"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls
                ]
            payload.append(entry)

        ollama_tools = (
            [
                {"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.parameters}}
                for t in tools
            ]
            if tools
            else None
        )
        return payload, ollama_tools, opts

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        options: dict[str, Any] | None = None,
        think: bool = False,
        preset: SamplingPreset = DETERMINISTIC,
    ) -> ChatResult:
        payload, ollama_tools, opts = self._build(messages, tools, options, preset)
        try:
            resp = await self._client.chat(
                model=self.s.chat_model, messages=payload, tools=ollama_tools, options=opts,
                think=think, stream=False, keep_alive=self.s.keep_alive,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed provider error
            raise ProviderError(f"ollama chat failed: {exc}") from exc

        msg = resp.message
        calls = [
            ToolCall(name=tc.function.name, arguments=dict(tc.function.arguments or {}))
            for tc in (msg.tool_calls or [])
        ]
        return ChatResult(
            content=msg.content or "", thinking=getattr(msg, "thinking", None), tool_calls=calls,
            tokens_in=resp.prompt_eval_count or 0, tokens_out=resp.eval_count or 0,
            model=self.s.chat_model,
        )

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        options: dict[str, Any] | None = None,
        think: bool = False,
        preset: SamplingPreset = DETERMINISTIC,
    ) -> AsyncIterator[ChatChunk]:
        payload, ollama_tools, opts = self._build(messages, tools, options, preset)
        content_acc = ""
        thinking_acc = ""
        calls: list[ToolCall] = []
        tokens_in = tokens_out = 0
        try:
            stream = await self._client.chat(
                model=self.s.chat_model, messages=payload, tools=ollama_tools, options=opts,
                think=think, stream=True, keep_alive=self.s.keep_alive,
            )
            async for part in stream:
                msg = part.message
                if msg.content:
                    content_acc += msg.content
                    yield ChatChunk(content=msg.content)
                delta = getattr(msg, "thinking", None)
                if delta:
                    thinking_acc += delta
                    yield ChatChunk(thinking=delta)
                for tc in (msg.tool_calls or []):
                    calls.append(ToolCall(name=tc.function.name, arguments=dict(tc.function.arguments or {})))
                if getattr(part, "done", False):
                    tokens_in = part.prompt_eval_count or 0
                    tokens_out = part.eval_count or 0
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"ollama chat stream failed: {exc}") from exc

        yield ChatChunk(
            done=True,
            result=ChatResult(
                content=content_acc, thinking=thinking_acc or None, tool_calls=calls,
                tokens_in=tokens_in, tokens_out=tokens_out, model=self.s.chat_model,
            ),
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            resp = await self._client.embed(
                model=self.s.embed_model, input=texts,
                options={"num_gpu": self.s.embed_num_gpu},  # CPU-only (fix H1/§M)
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"ollama embed failed: {exc}") from exc
        return [list(vec) for vec in resp.embeddings]

    def count_tokens(self, text: str) -> int:
        # Still a ~4-chars-per-token heuristic. Callers treat it as a conservative *upper bound*
        # (context engine pads by 1.2×), so it's safe; a real tokenizer would only tighten it.
        return max(1, len(text) // 4)

    async def health(self) -> bool:
        try:
            await self._client.list()
            return True
        except Exception:  # noqa: BLE001 - health probe never raises
            return False
