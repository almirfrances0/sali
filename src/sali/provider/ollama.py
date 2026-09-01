"""Ollama-backed provider (the current concrete backend).

Every machine-parsed call sends the full DETERMINISTIC option block explicitly so the
model's poisoned Modelfile defaults never leak in (fix H6). ``num_ctx`` is clamped well
below the model's advertised 262144 — unusable under 12 GB VRAM (fix H1/§M).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import shutil
import time
from collections.abc import AsyncIterator
from typing import Any

from ollama import AsyncClient

from sali.config.settings import ModelSettings
from sali.core.errors import ProviderError
from sali.obs.log import get_logger
from sali.provider.base import ChatChunk, ChatMessage, ChatResult, ToolCall, ToolSpec
from sali.provider.presets import DETERMINISTIC, SamplingPreset

log = get_logger("sali.provider.ollama")

# ── GPU safety: ONE generation at a time, machine-wide (prevents the card overloading into a shutdown) ─
# The 35B model does not fit a 12 GB card — it runs SPLIT (most layers on the GPU, the rest on the CPU),
# so a single generation already spikes GPU *and* CPU. TWO overlapping generations (the terminal + the
# `sali daemon`'s background loops, or vision on top of a chat) double that draw and were tripping the
# machine's power/thermal cutoff. So every model call — chat, streaming, vision, embeddings, foreground
# or background, in ANY Sali process — first takes a lease with three layers:
#   1. an in-process semaphore  (serialises coroutines within one process),
#   2. a resource gate          (defer while the card is too hot or its VRAM is near full — Sali knows
#                                its own usage and holds back instead of piling on),
#   3. a cross-process file lock (flock — serialises the terminal, the daemon, and every subagent so the
#                                heavy model is only ever generating ONCE machine-wide, never loaded twice;
#                                the kernel releases it automatically if a process dies mid-generation).
# A single inference briefly at ~100% GPU is normal and safe — the danger is CONCURRENCY, not one turn.
_INFERENCE = asyncio.Semaphore(1)
_GPU_TEMP_LIMIT_C = 85       # defer a NEW generation while the GPU is at least this hot (thermal margin)
_GPU_VRAM_CEILING = 0.92     # defer while the card is ≥92% full — never pile a new generation onto a
                             # card already near capacity (Almir's "only up to ~90%, then hold" rule)
_GPU_COOL_WAIT_S = 2.0       # re-check cadence while waiting for the card to settle
_GPU_MAX_WAITS = 30          # ~60s bounded backoff, then proceed anyway — never deadlock a turn
_LEASE_TIMEOUT_S = 900.0     # wait up to this for the cross-process lock, then proceed with a warning
# A well-known path so EVERY Sali process on this machine shares the same lock (terminal + daemon run as
# the same user). Auto-recreated if /tmp is cleared; the lock itself is the fd's flock, not the file.
_LOCK_PATH = "/tmp/sali-gpu-inference.lock"  # noqa: S108 - intentional machine-wide well-known lock file
_lock_fd: int | None = None


def _inference_lock_fd() -> int | None:
    """Open (once per process) the fd whose flock serialises GPU inference machine-wide. None if the
    lock can't be created — inference then degrades to the in-process semaphore only, never breaks."""
    global _lock_fd
    if _lock_fd is not None:
        return _lock_fd
    try:
        _lock_fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o666)
    except OSError as exc:
        log.warning("gpu_lock_unavailable", error=str(exc))
        _lock_fd = None
    return _lock_fd


async def _gpu_snapshot() -> tuple[int, float] | None:
    """(temperature °C, VRAM used fraction) via nvidia-smi, or None if unavailable. Read-only; a direct
    subprocess at the provider layer so nothing above needs a GPU dependency."""
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=temperature.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        temp_s, used_s, total_s = out.decode().strip().splitlines()[0].split(",")
        total = float(total_s) or 1.0
        return int(temp_s), float(used_s) / total
    except (OSError, ValueError, IndexError, TimeoutError):
        return None


async def _gpu_gate() -> None:
    """Hold a new generation back while the card is too hot OR its VRAM is near full, so Sali never
    piles onto an already-stressed card. Bounded — always proceeds after the backoff, never deadlocks."""
    for _ in range(_GPU_MAX_WAITS):
        snap = await _gpu_snapshot()
        if snap is None:
            return  # no GPU / no nvidia-smi → nothing to gate on
        temp, vram = snap
        if temp < _GPU_TEMP_LIMIT_C and vram < _GPU_VRAM_CEILING:
            return
        log.warning("gpu_busy_deferring_inference", temp_c=temp, vram_frac=round(vram, 3))
        await asyncio.sleep(_GPU_COOL_WAIT_S)


@contextlib.asynccontextmanager
async def _gpu_lease() -> AsyncIterator[None]:
    """Acquire the machine-wide single-generation lease for the duration of one model call: in-process
    semaphore → resource gate → cross-process flock. Guarantees the heavy model is only ever generating
    once across the terminal, the daemon, and every background loop — so it is never loaded/run twice."""
    async with _INFERENCE:
        await _gpu_gate()
        fd = _inference_lock_fd()
        held = False
        if fd is not None:
            deadline = time.monotonic() + _LEASE_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = True
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        log.warning("gpu_lease_timeout_proceeding")  # never deadlock a turn
                        break
                    await asyncio.sleep(0.15)  # another Sali process is generating — wait our turn
        try:
            yield
        finally:
            if held and fd is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)


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
            # Machine-wide single-generation lease — the GPU never runs two generations at once (§GPU safety).
            async with _gpu_lease():
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
        # Hold the machine-wide lease for the WHOLE stream (§GPU safety) — one generation at a time.
        async with _gpu_lease():
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

    async def describe_image(self, prompt: str, image: bytes) -> str:
        # Local vision: the screenshot goes ONLY to the local model (sali3 §24-25). Bypass _build
        # to attach the image to the message; low temperature for a faithful description.
        import base64

        try:
            # Vision is the SAME heavy multimodal model — take the lease so it never overlaps a chat
            # generation (previously it bypassed the guard entirely, a concurrency hole §GPU safety).
            async with _gpu_lease():
                resp = await self._client.chat(
                    model=self.s.chat_model,
                    messages=[{"role": "user", "content": prompt,
                               "images": [base64.b64encode(image).decode()]}],
                    options={"temperature": 0.2, "top_p": 0.9,
                             "num_ctx": min(self.s.ctx_default, self.s.ctx_max)},
                    think=False, stream=False, keep_alive=self.s.keep_alive,
                )
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed provider error
            raise ProviderError(f"ollama vision failed: {exc}") from exc
        return str(resp.message.content or "")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            # The embedder is a separate, CPU-only model (0 VRAM) — allowed to stay resident alongside
            # the chat model. It still takes the lease so a batch of embeddings never piles CPU load on
            # top of a chat generation that is already running its overflow layers on the CPU (§GPU safety).
            async with _gpu_lease():
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

    def context_limit(self) -> int:
        # The window Sali actually runs the model at — the same value clamped into num_ctx on every
        # call (§28). The budgeter reserves output room off this before deciding to compact.
        return min(self.s.ctx_default, self.s.ctx_max)

    async def health(self) -> bool:
        try:
            await self._client.list()
            return True
        except Exception:  # noqa: BLE001 - health probe never raises
            return False
