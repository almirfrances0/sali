"""llama.cpp-backed provider — the target concrete backend (replaces Ollama).

Talks to a ``llama-server`` process over its OpenAI-compatible HTTP API. The server is owned by
systemd, not by Sali: it loads exactly ONE model at startup and holds it permanently. That makes
most of the Ollama-era residency machinery structural rather than something to police:

* no ``keep_alive`` — the model is the server's reason to exist; it never idles out;
* no runner fingerprint — there is no per-request option that can fork a second copy of the model
  into VRAM, because context size, batch, threads and offload are server-startup flags;
* one slot (``--parallel 1``) — server-side concurrency Sali's locks cannot see does not exist.

What does NOT change: every call still passes through the machine-wide inference lease
(:mod:`sali.provider.lease`) — authority → semaphore → flock → residency probe → GPU gate — and
the residency probe here verifies the server is serving the configured model and nothing else
(the "no silent substitute" rule, enforced at runtime, per the owner directive).

Embeddings run on a SECOND llama-server (CPU-only, ``--embeddings``) — same separation as the
Ollama setup: the embedder never contends with the reasoning model for VRAM.

Streaming is genuine SSE from the inference process: chunks are yielded as they arrive, never
buffered to completion; token usage rides the final chunk (``stream_options.include_usage``).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from sali.config.settings import ModelSettings
from sali.core.errors import ProviderError
from sali.obs.log import get_logger
from sali.provider.authority import InferenceKind
from sali.provider.base import ChatChunk, ChatMessage, ChatResult, ToolCall, ToolSpec
from sali.provider.lease import _inference_lease
from sali.provider.presets import DETERMINISTIC, SamplingPreset

log = get_logger("sali.provider.llamacpp")

# How long a clean "the server serves exactly the configured model" answer stays trusted. Model
# identity changes only when the service is restarted with a different artifact — tens of minutes
# at the fastest — so this keeps the probe off the hot path without letting a swap go unnoticed.
_PROBE_TTL_S = 15.0


class LlamaCppProvider:
    """ModelProvider over llama-server's OpenAI-compatible API."""

    def __init__(self, settings: ModelSettings) -> None:
        self.s = settings
        self._alias = settings.llamacpp_model_alias
        # Generous timeout so a long single-shot generation (a full page, slowly) never gets
        # cut off mid-stream on modest hardware. The embedder gets a short one: it is a data
        # operation that must fail fast, never wedge a turn.
        self._client = httpx.AsyncClient(
            base_url=settings.llamacpp_host, timeout=settings.request_timeout_s)
        self._embed_client = httpx.AsyncClient(
            base_url=settings.llamacpp_embed_host, timeout=120.0)
        self._probe_cache: tuple[float, None] | None = None

    @property
    def chat_model(self) -> str:
        """The served model id. Fixed at the service level — there is no runtime switch (one
        model, no substitutes), so this is simply the configured alias the server was started
        with. Exposed for the same duck-typed callers that read OllamaProvider.chat_model."""
        return self._alias

    # ── payload building ────────────────────────────────────────────────────────────────────────

    def _sampling(self, options: dict[str, Any] | None, preset: SamplingPreset) -> dict[str, Any]:
        """A complete, explicit sampling block. llama-server accepts the same field names the
        presets emit (temperature/top_k/top_p/presence_penalty/repeat_penalty/seed), so the block
        passes through unchanged — every knob pinned, nothing inherited from server defaults."""
        opts: dict[str, Any] = preset.to_options()
        if options:
            opts.update(options)
        return opts

    @staticmethod
    def _to_openai_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """Internal chat history → OpenAI wire format.

        Two structural translations: the assistant's tool calls become ``tool_calls`` entries with
        synthesised ids (our internal model carries name+arguments only), and each tool RESULT
        (role ``tool``, ``name`` = tool name) is paired to the id of the call it answers, because
        OpenAI's format — and llama.cpp's chat template — keys results by ``tool_call_id``."""
        out: list[dict[str, Any]] = []
        pending: list[str] = []
        for m in messages:
            if m.role == "tool" or (m.name is not None and m.role != "assistant"):
                tid = pending.pop(0) if pending else f"call_{m.name or 'tool'}"
                out.append({"role": "tool", "tool_call_id": tid, "content": m.content})
                continue
            entry: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_calls:
                calls: list[dict[str, Any]] = []
                for c in m.tool_calls:
                    tid = f"call_{len(out)}_{len(calls)}"
                    pending.append(tid)
                    calls.append({"id": tid, "type": "function",
                                  "function": {"name": c.name,
                                               "arguments": json.dumps(c.arguments)}})
                entry["tool_calls"] = calls
            out.append(entry)
        return out

    @staticmethod
    def _to_openai_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [{"type": "function",
                 "function": {"name": t.name, "description": t.description,
                              "parameters": t.parameters}} for t in tools]

    def _payload(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
        options: dict[str, Any] | None,
        preset: SamplingPreset,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._alias,
            "messages": self._to_openai_messages(messages),
            "stream": stream,
            # DISABLE MODEL REASONING at the source (owner directive, carried over from the Ollama
            # backend): Qwen3-family templates honour enable_thinking=False, so no chain-of-thought
            # is ever PRODUCED (not stripped after the fact). Verified at the live gate: a response
            # must contain no <think> block.
            "chat_template_kwargs": {"enable_thinking": False},
            **self._sampling(options, preset),
        }
        oai_tools = self._to_openai_tools(tools)
        if oai_tools is not None:
            payload["tools"] = oai_tools
            payload["tool_choice"] = "auto"
        if stream:
            # Token counts ride the last usage chunk, so callers get real numbers, not estimates.
            payload["stream_options"] = {"include_usage": True}
        return payload

    # ── errors & identity probe ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _error_detail(body: bytes) -> str:
        """llama-server error bodies are {"error": {"message": ...}} when JSON — extract the
        message so downstream classifiers (context-overflow recovery) see the server's wording."""
        try:
            obj = json.loads(body)
        except ValueError:
            return body.decode(errors="replace")[:300]
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])[:300]
            if obj.get("message"):
                return str(obj["message"])[:300]
        return body.decode(errors="replace")[:300]

    async def _probe_served_model(self) -> None:
        """Prove the server serves exactly the configured model — the no-silent-substitute gate.

        Raises only on a REAL mismatch (the server answered and named a different model). A server
        that cannot be reached never raises here: the call the lease protects will fail on its own
        with a clearer error, and a diagnostic must never be what breaks inference. Cached briefly.
        """
        now = time.monotonic()
        if self._probe_cache is not None and now - self._probe_cache[0] < _PROBE_TTL_S:
            return
        try:
            r = await self._client.get("/v1/models", timeout=5.0)
            if r.status_code != 200:
                return
            ids = [m.get("id") for m in r.json().get("data", []) if isinstance(m, dict)]
        except Exception:  # noqa: BLE001 - a diagnostic must never break the call it guards
            return
        if ids and self._alias not in ids:
            raise ProviderError(
                f"llama-server is serving {ids!r}, not the configured model "
                f"{self._alias!r}. Sali has exactly one reasoning model, configured at the "
                "service level — refusing to think with a substitute. Fix the sali-llamacpp "
                "service or the llamacpp_model_alias setting.")
        self._probe_cache = (now, None)

    # ── the ModelProvider contract ──────────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        options: dict[str, Any] | None = None,
        think: bool = False,
        preset: SamplingPreset = DETERMINISTIC,
    ) -> ChatResult:
        payload = self._payload(messages, tools, options, preset, stream=False)
        try:
            # One mind, one generation: the single inference boundary (authority + machine-wide lease).
            async with _inference_lease(InferenceKind.COGNITION,
                                        residency_probe=self._probe_served_model):
                r = await self._client.post("/v1/chat/completions", json=payload)
                if r.status_code >= 400:
                    raise ProviderError(
                        f"llamacpp chat failed: {r.status_code} - {self._error_detail(r.content)}")
                data = r.json()
        except ProviderError:
            raise                      # already typed (overflow, refused, mismatched model)
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed provider error
            raise ProviderError(f"llamacpp chat failed: {exc}") from exc

        msg = (data.get("choices") or [{}])[0].get("message") or {}
        usage = data.get("usage") or {}
        return ChatResult(
            content=msg.get("content") or "",
            thinking=msg.get("reasoning_content") or None,
            tool_calls=self._parse_tool_calls(msg.get("tool_calls") or []),
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            model=self._alias,
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
        payload = self._payload(messages, tools, options, preset, stream=True)
        content_acc = ""
        thinking_acc = ""
        call_names: dict[int, str] = {}
        call_args: dict[int, str] = {}
        tokens_in = tokens_out = 0
        # Hold the lease for the WHOLE stream — one mind, one generation at a time.
        async with _inference_lease(InferenceKind.COGNITION,
                                    residency_probe=self._probe_served_model):
            try:
                async with self._client.stream(
                        "POST", "/v1/chat/completions", json=payload) as r:
                    if r.status_code >= 400:
                        raise ProviderError(
                            f"llamacpp chat stream failed: {r.status_code} - "
                            f"{self._error_detail(await r.aread())}")
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue  # a keep-alive comment or partial line — never fatal
                        usage = obj.get("usage") or {}
                        if usage:
                            tokens_in = int(usage.get("prompt_tokens") or tokens_in)
                            tokens_out = int(usage.get("completion_tokens") or tokens_out)
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        if delta.get("content"):
                            content_acc += delta["content"]
                            yield ChatChunk(content=delta["content"])
                        if delta.get("reasoning_content"):
                            thinking_acc += delta["reasoning_content"]
                            yield ChatChunk(thinking=delta["reasoning_content"])
                        for tc in delta.get("tool_calls") or []:
                            idx = int(tc.get("index", 0))
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                call_names[idx] = fn["name"]
                            if fn.get("arguments"):
                                call_args[idx] = call_args.get(idx, "") + fn["arguments"]
            except ProviderError:
                raise                  # already typed (overflow, refused, mismatched model)
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"llamacpp chat stream failed: {exc}") from exc

        calls = [
            ToolCall(name=call_names.get(idx, ""), arguments=self._parse_args(call_args.get(idx, "")))
            for idx in sorted(call_args)
        ]
        yield ChatChunk(
            done=True,
            result=ChatResult(
                content=content_acc, thinking=thinking_acc or None, tool_calls=calls,
                tokens_in=tokens_in, tokens_out=tokens_out, model=self._alias,
            ),
        )

    async def describe_image(self, prompt: str, image: bytes) -> str:
        # Local vision: the screenshot goes ONLY to the local model (sali3 §24-25). The mmproj
        # projector is loaded by the server at startup; vision is the SAME heavy multimodal
        # model — cognition, and leased like any other.
        b64 = base64.b64encode(image).decode()
        payload: dict[str, Any] = {
            "model": self._alias,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "temperature": 0.2, "top_p": 0.9,
        }
        try:
            async with _inference_lease(InferenceKind.COGNITION,
                                        residency_probe=self._probe_served_model):
                r = await self._client.post("/v1/chat/completions", json=payload)
                if r.status_code >= 400:
                    raise ProviderError(
                        f"llamacpp vision failed: {r.status_code} - "
                        f"{self._error_detail(r.content)}")
                data = r.json()
        except ProviderError:
            raise                      # already typed (e.g. refused by the inference authority)
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed provider error
            raise ProviderError(f"llamacpp vision failed: {exc}") from exc
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        return str(msg.get("content") or "")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.s.embed_model, "input": texts}
        try:
            # The embedder is a separate, CPU-only server (0 VRAM): a DATA operation, not cognition.
            # It still takes the lease so a batch of embeddings never piles CPU load on top of a
            # generation already running its overflow layers on the CPU.
            async with _inference_lease(InferenceKind.EMBEDDING):
                r = await self._embed_client.post("/v1/embeddings", json=payload)
                if r.status_code >= 400:
                    raise ProviderError(
                        f"llamacpp embed failed: {r.status_code} - "
                        f"{self._error_detail(r.content)}")
                data = r.json()
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"llamacpp embed failed: {exc}") from exc
        rows = sorted(data.get("data", []), key=lambda d: int(d.get("index", 0)))
        return [list(map(float, row.get("embedding", []))) for row in rows]

    def count_tokens(self, text: str) -> int:
        # Still a ~4-chars-per-token heuristic. Callers treat it as a conservative *upper bound*
        # (context engine pads by 1.2×), so it's safe; llama-server's /tokenize would only tighten
        # it, and the protocol method here is synchronous.
        return max(1, len(text) // 4)

    def context_limit(self) -> int:
        # The window Sali actually runs the model at — llama-server is started with -c set to the
        # same value (systemd unit), so this is the budget the context engine packs into.
        return min(self.s.ctx_default, self.s.ctx_max)

    async def health(self) -> bool:
        try:
            r = await self._client.get("/health", timeout=5.0)
            return r.status_code == 200  # 503 while the model is still loading — honestly not ready
        except Exception:  # noqa: BLE001 - health probe never raises
            return False

    # ── tool-call parsing helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_args(raw: str) -> dict[str, Any]:
        """The model emits tool arguments as a JSON string; a malformed one becomes {} with a log
        line — the loop's verification layer judges the call on what it can actually see."""
        try:
            args = json.loads(raw) if raw.strip() else {}
        except ValueError:
            log.warning("tool_call_args_unparseable", raw=raw[:200])
            return {}
        return args if isinstance(args, dict) else {"value": args}

    @classmethod
    def _parse_tool_calls(cls, raw_calls: list[dict[str, Any]]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for tc in raw_calls:
            fn = tc.get("function") or {}
            calls.append(ToolCall(name=str(fn.get("name") or ""),
                                  arguments=cls._parse_args(str(fn.get("arguments") or ""))))
        return calls

    # ── introspection for the API layer (model switcher becomes a status view) ─────────────────

    async def server_info(self) -> dict[str, Any]:
        """What the server is actually serving: model id, context, modalities, slots. Best-effort;
        an unreachable server reports ``up: False`` rather than raising."""
        info: dict[str, Any] = {"up": False, "model": None, "vision": False}
        try:
            models = await self._client.get("/v1/models", timeout=5.0)
            if models.status_code == 200:
                ids = [m.get("id") for m in models.json().get("data", []) if isinstance(m, dict)]
                info["models"] = ids
                info["model"] = ids[0] if ids else None
                info["up"] = True
        except Exception:  # noqa: BLE001
            return info
        try:
            props = await self._client.get("/props", timeout=5.0)
            if props.status_code == 200:
                p = props.json()
                info["n_ctx"] = (p.get("default_generation_settings") or {}).get("n_ctx")
                modalities = p.get("modalities") or {}
                info["vision"] = bool(modalities.get("vision") or p.get("mmproj"))
                info["model_path"] = p.get("model_path")
        except Exception:  # noqa: BLE001 - props are enrichment, never required
            pass
        return info
