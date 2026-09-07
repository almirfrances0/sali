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
from sali.provider.authority import InferenceKind, admit
from sali.provider.base import ChatChunk, ChatMessage, ChatResult, ToolCall, ToolSpec
from sali.provider.presets import DETERMINISTIC, SamplingPreset

log = get_logger("sali.provider.ollama")

# Ceiling for a single best-effort residency call (survey / evict-generate) in ensure_only_loaded. The
# shared client's timeout is sized for a slow full-page generation; a HEALTH/eviction call must fail
# fast so a stalled ollama can never wedge the daemon's boot (it is awaited during startup).
_EVICT_CALL_TIMEOUT_S = 8.0

# ── The inference boundary: ONE mind, ONE generation at a time, machine-wide ──────────────────────────
# The 35B model does not fit a 12 GB card — it runs SPLIT (most layers on the GPU, the rest on the CPU),
# so a single generation already spikes GPU *and* CPU. TWO overlapping generations (the terminal + the
# `sali daemon`'s background loops, or vision on top of a chat) double that draw and were tripping the
# machine's power/thermal cutoff. So every model call — chat, streaming, vision, embeddings, foreground
# or background, in ANY Sali process — passes through _inference_lease(), which has four layers:
#   0. the INFERENCE AUTHORITY  (sali.provider.authority — may this PROCESS think at all? Cognition is
#                                the one living mind's privilege; a non-mind process is refused, not
#                                queued, so a second reasoning path can never quietly open),
#   1. an in-process semaphore  (serialises coroutines within one process),
#   2. a cross-process file lock (flock — serialises the terminal, the daemon, and every faculty so the
#                                heavy model is only ever generating ONCE machine-wide; the kernel
#                                releases it automatically if a process dies mid-generation),
#   3. a resource gate          (defer while the card is too hot or its VRAM is near full — read AFTER
#                                the lock is won, so the reading describes the card we are about to
#                                use rather than one someone else has since heated).
# A single inference briefly at ~100% GPU is normal and safe — the danger is CONCURRENCY, not one turn.
# Note on residency: Ollama keys a runner by (model, runner-affecting options). Every call here sends
# the SAME num_ctx (see _build/describe_image), so the chat model resolves to ONE runner however many
# callers there are; the CPU-only embedder is the deliberate second. Duplicate residency therefore needs
# a duplicate *option set*, which is why num_ctx is centralised and never overridden by callers.
_INFERENCE = asyncio.Semaphore(1)
_GPU_TEMP_LIMIT_C = 85       # defer a NEW generation while the GPU is at least this hot (thermal margin)
# MEASURED 2026-09-02: sali:latest alone resides at ~90.9% of this 12 GB card (size_vram 10.07 GB with
# num_ctx 24576). A 0.92 ceiling therefore left ~135 MiB of headroom and made Sali gate against its OWN
# steady state — any transient nudged it over and EVERY generation paid the full ~60s backoff below (the
# real cause of "Sali is slow", and of an apparent outage where the API never bound during startup).
# The rule's intent is "don't pile a NEW generation onto a card already near capacity"; 0.95 preserves
# that (still ~4 points / ~500 MiB of genuine slack above baseline) without gating normal operation.
# 0.985, not a "safe-looking" number, because on THIS box VRAM fullness is not a risk signal at all:
# sali:latest is a RESIDENT 35B on a 12 GB card and its footprint GROWS with the KV cache (measured
# 90.9% -> 93.6% -> 95.05% within one session). Every ceiling below ~100% therefore gets crossed
# eventually, and when it is crossed Sali stalls against ITSELF — 88 `gpu_busy_deferring_inference`
# lines in five minutes while the card sat at 35-40 degrees. The genuine protections are independent of
# this number and remain in force: `assert_single_residency` (a SECOND copy of the model is the actual
# emergency), the temperature gate above (85 degrees, versus the 35-40 seen in normal work), the
# machine-wide inference flock, and the sali-power-envelope service that caps package + GPU power. What
# 0.985 still catches is the case this gate was written for: something OTHER than Sali's one model
# eating the card.
_GPU_VRAM_CEILING = 0.985
_GPU_COOL_WAIT_S = 2.0       # re-check cadence while waiting for the card to settle
_GPU_MAX_WAITS = 30          # ~60s bounded backoff, then proceed anyway — never deadlock a turn
_LEASE_TIMEOUT_S = 900.0     # wait up to this for the cross-process lock, then FAIL (never double up)
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


_GPU_SNAP_AT = 0.0
_GPU_SNAP_VAL: tuple[int, float] | None = None


async def gpu_pressure(*, max_age_s: float = 60.0) -> tuple[int, float] | None:
    """(temperature °C, VRAM used fraction) for callers OUTSIDE the lease, from the same cached reading.

    Public because Sali needs to be able to NOTICE the state of the card he depends on, not only be
    stopped by it. Every generation refreshes this cache, so during active use it is warm and free —
    which is the point: a check that costs a subprocess before every reply would be paid on thousands
    of healthy turns to catch the rare bad one."""
    return await _gpu_snapshot(max_age_s=max_age_s)


async def _gpu_snapshot(*, max_age_s: float = 0.0) -> tuple[int, float] | None:
    """(temperature °C, VRAM used fraction) via nvidia-smi, or None if unavailable. Read-only; a direct
    subprocess at the provider layer so nothing above needs a GPU dependency. A small ``max_age_s``
    lets the several pre-token lease acquisitions of one turn share a reading instead of paying a
    subprocess exec each (audit) — resource decisions tolerate a few seconds of staleness."""
    global _GPU_SNAP_AT, _GPU_SNAP_VAL
    if max_age_s > 0 and (time.monotonic() - _GPU_SNAP_AT) < max_age_s:
        return _GPU_SNAP_VAL
    snap: tuple[int, float] | None = None
    if shutil.which("nvidia-smi") is not None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            temp_s, used_s, total_s = out.decode().strip().splitlines()[0].split(",")
            total = float(total_s) or 1.0
            snap = (int(temp_s), float(used_s) / total)
        except (OSError, ValueError, IndexError, TimeoutError):
            snap = None
    _GPU_SNAP_AT = time.monotonic()
    _GPU_SNAP_VAL = snap
    return snap


async def _gpu_gate() -> None:
    """Hold a new generation back while the card is too hot OR its VRAM is near full, so Sali never
    piles onto an already-stressed card. Bounded — always proceeds after the backoff, never deadlocks."""
    for i in range(_GPU_MAX_WAITS):
        snap = await _gpu_snapshot(max_age_s=(5.0 if i == 0 else 0.0))
        if snap is None:
            return  # no GPU / no nvidia-smi → nothing to gate on
        temp, vram = snap
        if temp < _GPU_TEMP_LIMIT_C and vram < _GPU_VRAM_CEILING:
            return
        log.warning("gpu_busy_deferring_inference", temp_c=temp, vram_frac=round(vram, 3))
        await asyncio.sleep(_GPU_COOL_WAIT_S)


@contextlib.asynccontextmanager
async def _inference_lease(
    kind: InferenceKind, *, client: Any = None, chat_model: str = "",
) -> AsyncIterator[None]:
    """THE inference boundary. Every model call in Sali passes through here, in this order:

        authority → in-process semaphore → machine-wide flock → residency proof → resource gate

    Each layer answers a different question, and none of the others can answer it:

    * **authority** — may this PROCESS think at all? (one mind)
    * **semaphore + flock** — may it think RIGHT NOW? (one generation at a time, machine-wide)
    * **residency** — is the model loaded exactly ONCE? Locks constrain Sali; they say nothing
      about what the ollama server is holding, and a second copy of a 16 GB MoE on a 12 GB card is
      a host-safety emergency, not a slowdown.
    * **resource gate** — is the card fit to be used at all? Read last, so the measurement
      describes the card we are about to use rather than one someone else has since heated.

    Putting them in one place is deliberate: a new provider method cannot acquire the GPU lease —
    which it visibly must — without also passing every other gate.
    """
    admit(kind)
    async with _INFERENCE:
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
                        # Never "proceed anyway": that trades a stalled turn for two concurrent
                        # generations — the exact condition that powers this machine off. Fail the
                        # call honestly; the caller retries or surfaces it.
                        raise ProviderError(
                            f"inference lease timed out after {_LEASE_TIMEOUT_S:.0f}s — another "
                            "generation still holds the machine-wide GPU lease. Refusing to run a "
                            "second generation concurrently (host safety). Check `sali status`."
                        ) from None
                    await asyncio.sleep(0.15)  # another Sali process is generating — wait our turn
        # Proof, not faith, that the model is loaded once — checked while we hold the lock, so no
        # other generation can change residency between the check and the call it protects.
        if client is not None and chat_model and kind is InferenceKind.COGNITION:
            from sali.provider.residency import assert_single_residency
            await assert_single_residency(client, chat_model)
        # Read the card AFTER winning the lock, not before. Gating first would have measured an idle
        # card, then waited minutes behind someone else's generation and started onto the hot card
        # that generation left behind — a stale reading is worse than none. Holding the lock while
        # the card settles is the point: nobody else should start either.
        if kind is not InferenceKind.EMBEDDING:  # embeds are CPU-only (0 VRAM) — nothing to gate on
            await _gpu_gate()
        try:
            yield
        finally:
            if held and fd is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)


class OllamaProvider:
    def __init__(self, settings: ModelSettings) -> None:
        self.s = settings
        # Runtime model override: the owner can pick the active chat model from the app (the model
        # switcher). None = use the configured default. Persisted in sali_state and reloaded at
        # startup, so the choice survives restarts. Still ONE active model at a time — the owner just
        # chooses which one, so the one-mind invariant holds.
        self._model_override: str | None = None
        # Generous timeout so a long single-shot generation (a full page, slowly) never gets
        # cut off mid-stream on modest hardware.
        self._client = AsyncClient(host=settings.host, timeout=settings.request_timeout_s)

    @property
    def chat_model(self) -> str:
        """The ACTIVE chat model — the owner's runtime override if set, else the configured default.
        Every chat/stream/residency call routes through this so a switch takes effect immediately."""
        return self._model_override or self.s.chat_model

    def set_active_model(self, name: str | None) -> None:
        """Point the provider at a different installed model (owner-driven switch). Empty/None clears
        the override back to the configured default."""
        self._model_override = (name or "").strip() or None

    async def ensure_only_loaded(self, keep: str) -> list[str]:
        """Evict every model ollama currently holds EXCEPT ``keep`` and the CPU embedder — so a model
        switch can never leave the previous 30B resident next to the new one. That is fatal on a 12 GB
        card and genuinely possible here: OLLAMA_MAX_LOADED_MODELS=2 lets two runners coexist, and
        Sali's own keep_alive="24h" pins the old model rather than letting it idle out. ``keep_alive=0``
        tells ollama to release a runner immediately.

        Returns the models it CONFIRMED gone (verified by re-survey — not by whether the unload call
        returned cleanly, because an empty-prompt generate unloads the runner server-side but can still
        raise while the client parses the empty response). Retries once, since a runner busy finishing
        a pull may ignore the first request. Called on every switch and at startup after restoring a
        saved choice, so exactly one large model is ever resident — the active one. Never raises.
        """
        from sali.provider.residency import invalidate, normalize_ref, survey

        # COMPARE THE TAG. This was a base-name comparison, which made every `sali:*` tag look like
        # the model being kept — so switching sali:latest -> sali:pro evicted NOTHING and left the
        # previous 30B pinned (keep_alive=24h) beside the new one: precisely the two-large-runners
        # state this method exists to prevent, and which browned the host out twice on 2026-09-01.
        # Tags are quantisations here (Q2_K_P / IQ2_M / IQ4_XS), not aliases.
        keep_ref = normalize_ref(keep or "")
        embed_ref = normalize_ref(self.s.embed_model or "")

        def _foreign(runners: Any) -> list[str]:
            return [r.model for r in runners
                    if normalize_ref(r.model) not in (keep_ref, embed_ref)]

        # BOUND every ollama call here. This is best-effort eviction (the switch path and vram-janitor
        # also evict later), but it is awaited at STARTUP — and the shared client's timeout is sized for
        # a slow full-page generation, not a health call. A single slow /api/ps or generate here once
        # wedged boot for minutes with no port bound (mind_acquired logged, "Sali is up" never reached).
        # A short per-call ceiling means a stalled model server can never hold the daemon's boot hostage:
        # a timeout is swallowed like any other failure and eviction is simply skipped this pass.
        confirmed: list[str] = []
        for attempt in range(2):
            targets: list[str] = []
            with contextlib.suppress(Exception):
                _survey = await asyncio.wait_for(survey(self._client, keep), _EVICT_CALL_TIMEOUT_S)
                targets = _foreign(_survey.runners)
            if not targets:
                break
            for m in targets:
                with contextlib.suppress(Exception):
                    # keep_alive=0 releases the runner; the client may still raise on the empty
                    # response, so we DON'T trust its return — we re-survey below.
                    await asyncio.wait_for(
                        self._client.generate(model=m, prompt="", keep_alive=0), _EVICT_CALL_TIMEOUT_S)
            invalidate()
            await asyncio.sleep(1.0 if attempt == 0 else 2.0)
            with contextlib.suppress(Exception):
                _resurvey = await asyncio.wait_for(survey(self._client, keep), _EVICT_CALL_TIMEOUT_S)
                still = {r.model for r in _resurvey.runners}
                confirmed = [m for m in targets if m not in still]
                still_foreign = [m for m in still
                                 if normalize_ref(m) not in (keep_ref, embed_ref)]
                if not still_foreign:
                    return confirmed  # nothing foreign left — done
        return confirmed

    def _runner_options(self) -> dict[str, Any]:
        """The options that decide WHICH runner ollama uses — the model's residency fingerprint.

        Ollama keys a loaded runner by (model, runner-affecting options). Two calls that differ in
        any of these get TWO runners, i.e. the 35B loaded twice on a 12GB card. So this is the single
        source of those values, and it is stamped over caller options rather than merged under them:
        a caller may tune sampling (temperature, top_k) freely and can never, by any route, fork a
        second copy of the model into VRAM.
        """
        return {
            "num_ctx": min(self.s.ctx_default, self.s.ctx_max),
            "num_batch": self.s.num_batch,     # prefill batch — the power-transient lever
            "num_thread": self.s.num_thread,   # CPU threads for offloaded layers
        }

    def _build(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
        options: dict[str, Any] | None,
        preset: SamplingPreset,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, dict[str, Any]]:
        opts: dict[str, Any] = preset.to_options()
        if options:
            opts.update(options)
        opts.update(self._runner_options())  # authoritative — LAST, so nothing can fork a runner

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

        # DISABLE MODEL REASONING at the source (owner directive: reasoning makes the model hallucinate).
        # Some models — Qwen3-family GGUFs — IGNORE Ollama's think=False and generate chain-of-thought
        # anyway. Appending /no_think to the system prompt turns thinking OFF at the model level, so no
        # reasoning is ever PRODUCED (not stripped after the fact). It is inert noise for a model that
        # doesn't think, so it is safe to send to every model. Paired with think=False on every call.
        _sys = next((e for e in payload if e.get("role") == "system"), None)
        if _sys is not None:
            _sys["content"] = f"{_sys.get('content') or ''} /no_think".strip()
        else:
            # No system message present: inserting a NEW system entry would OVERRIDE the model's own
            # Modelfile SYSTEM directive. Append /no_think to the last user message instead (the model
            # reads it anywhere in the prompt), so the reasoning-off toggle never clobbers the system.
            _lastu = next((e for e in reversed(payload) if e.get("role") == "user"), None)
            if _lastu is not None:
                _lastu["content"] = f"{_lastu.get('content') or ''} /no_think".strip()
            elif payload:
                payload.insert(0, {"role": "system", "content": "/no_think"})

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
            # One mind, one generation: the single inference boundary (authority + machine-wide lease).
            async with _inference_lease(InferenceKind.COGNITION,
                                        client=self._client, chat_model=self.chat_model):
                resp = await self._client.chat(
                    model=self.chat_model, messages=payload, tools=ollama_tools, options=opts,
                    think=False, stream=False, keep_alive=self.s.keep_alive,  # reasoning HARD-OFF (owner)
                )
        except ProviderError:
            raise                      # already typed (e.g. refused by the inference authority)
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
            model=self.chat_model,
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
        # Hold the lease for the WHOLE stream (§GPU safety) — one mind, one generation at a time.
        async with _inference_lease(InferenceKind.COGNITION,
                                    client=self._client, chat_model=self.chat_model):
            try:
                stream = await self._client.chat(
                    model=self.chat_model, messages=payload, tools=ollama_tools, options=opts,
                    think=False, stream=True, keep_alive=self.s.keep_alive,  # reasoning HARD-OFF (owner)
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
            except ProviderError:
                raise                  # already typed (e.g. refused by the inference authority)
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"ollama chat stream failed: {exc}") from exc

        yield ChatChunk(
            done=True,
            result=ChatResult(
                content=content_acc, thinking=thinking_acc or None, tool_calls=calls,
                tokens_in=tokens_in, tokens_out=tokens_out, model=self.chat_model,
            ),
        )

    async def describe_image(self, prompt: str, image: bytes) -> str:
        # Local vision: the screenshot goes ONLY to the local model (sali3 §24-25). Bypass _build
        # to attach the image to the message; low temperature for a faithful description.
        import base64

        try:
            # Vision is the SAME heavy multimodal model — cognition, and leased like any other, so it
            # never overlaps a chat generation nor runs outside the one mind.
            async with _inference_lease(InferenceKind.COGNITION,
                                        client=self._client, chat_model=self.chat_model):
                resp = await self._client.chat(
                    model=self.chat_model,
                    messages=[{"role": "user", "content": prompt,
                               "images": [base64.b64encode(image).decode()]}],
                    options={"temperature": 0.2, "top_p": 0.9, **self._runner_options()},
                    think=False, stream=False, keep_alive=self.s.keep_alive,
                )
        except ProviderError:
            raise                      # already typed (e.g. refused by the inference authority)
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed provider error
            raise ProviderError(f"ollama vision failed: {exc}") from exc
        return str(resp.message.content or "")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            # The embedder is a separate, CPU-only model (0 VRAM): a DATA operation, not cognition, so a
            # utility process may run it. It still takes the lease so a batch of embeddings never piles
            # CPU load on top of a generation already running its overflow layers on the CPU.
            async with _inference_lease(InferenceKind.EMBEDDING):
                resp = await self._client.embed(
                    model=self.s.embed_model, input=texts,
                    # Without this it inherited Ollama's 5-minute default and was evicted between
                    # conversations, turning the first retrieval after any pause into a ~7s model load.
                    keep_alive=self.s.embed_keep_alive,
                    # CPU-only (0 VRAM) and thread-capped like everything else, so a batch of
                    # embeddings cannot add an all-core spike on top of an offloaded generation.
                    options={"num_gpu": self.s.embed_num_gpu, "num_thread": self.s.num_thread},
                )
        except ProviderError:
            raise
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
