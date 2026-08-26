"""Ollama-backed provider (the current concrete backend).

Every machine-parsed call sends the full DETERMINISTIC option block explicitly so the
model's poisoned Modelfile defaults never leak in (fix H6). ``num_ctx`` is clamped well
below the model's advertised 262144 — unusable under 12 GB VRAM (fix H1/§M).
"""

from __future__ import annotations

from typing import Any

from ollama import AsyncClient

from sali.config.settings import ModelSettings
from sali.core.errors import ProviderError
from sali.provider.base import ChatMessage, ChatResult, ToolCall, ToolSpec
from sali.provider.presets import DETERMINISTIC, SamplingPreset


class OllamaProvider:
    def __init__(self, settings: ModelSettings) -> None:
        self.s = settings
        self._client = AsyncClient(host=settings.host)

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        options: dict[str, Any] | None = None,
        think: bool = False,
        preset: SamplingPreset = DETERMINISTIC,
    ) -> ChatResult:
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
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            if tools
            else None
        )

        try:
            resp = await self._client.chat(
                model=self.s.chat_model,
                messages=payload,
                tools=ollama_tools,
                options=opts,
                think=think,
                stream=False,
                keep_alive="5m",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed provider error
            raise ProviderError(f"ollama chat failed: {exc}") from exc

        msg = resp.message
        calls = [
            ToolCall(name=tc.function.name, arguments=dict(tc.function.arguments or {}))
            for tc in (msg.tool_calls or [])
        ]
        return ChatResult(
            content=msg.content or "",
            thinking=getattr(msg, "thinking", None),
            tool_calls=calls,
            tokens_in=resp.prompt_eval_count or 0,
            tokens_out=resp.eval_count or 0,
            model=self.s.chat_model,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            resp = await self._client.embed(
                model=self.s.embed_model,
                input=texts,
                options={"num_gpu": self.s.embed_num_gpu},  # CPU-only (fix H1/§M)
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"ollama embed failed: {exc}") from exc
        return [list(vec) for vec in resp.embeddings]

    def count_tokens(self, text: str) -> int:
        # Phase-0 heuristic; a bundled HF tokenizer replaces this in Phase 4 (fix H7).
        return max(1, len(text) // 4)

    async def health(self) -> bool:
        try:
            await self._client.list()
            return True
        except Exception:  # noqa: BLE001 - health probe never raises
            return False
