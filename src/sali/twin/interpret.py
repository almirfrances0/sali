"""LLM-assisted authority interpretation (spec §34/§35).

The deterministic classifier (twin.authority) knows the obvious dangerous binaries by name. For an
unfamiliar tool, Sali can ask the local model for a second opinion on how dangerous it is — but under
a strict rule that honours §35 ("never rely on the model to police itself"): the model may only
ESCALATE a tool to a more cautious tier, NEVER lower the deterministic floor. So a model that
mistakenly calls `mkfs` "normal" changes nothing, while a model that flags an obscure wiper as
system-critical adds protection. On any parse failure the deterministic classification stands.
"""

from __future__ import annotations

from sali.core.enums import ExecAuthority
from sali.core.toolvocab import CAPABILITY_VOCAB
from sali.provider.presets import DETERMINISTIC
from sali.provider.base import ChatMessage, ModelProvider
from sali.twin.authority import classify_authority

_SEVERITY = {ExecAuthority.NORMAL: 0, ExecAuthority.ELEVATED: 1, ExecAuthority.SYSTEM_CRITICAL: 2}
_SYSTEM = (
    "You judge how dangerous a Linux command-line tool is to run, as exactly one word:\n"
    "- normal: everyday, safe (ls, jq, curl, grep)\n"
    "- elevated: needs root or changes system state (apt, systemctl, mount, iptables)\n"
    "- system_critical: can irreversibly destroy data, disks, or the system (mkfs, fdisk, dd, shred)\n"
    "Reply with ONLY that one word."
)
# Brain-audit Turn 6: _OPTS deleted. Twin extractor uses preset=DETERMINISTIC by
# design (structured JSON extractor needs stable output).


def _parse_tier(answer: str) -> ExecAuthority | None:
    a = answer.strip().lower()
    if "critical" in a:
        return ExecAuthority.SYSTEM_CRITICAL
    if "elevated" in a:
        return ExecAuthority.ELEVATED
    if "normal" in a:
        return ExecAuthority.NORMAL
    return None


async def _ask_model(provider: ModelProvider, name: str, synopsis: str) -> ExecAuthority | None:
    user = f"Tool: {name}" + (f"\nWhat it does: {synopsis}" if synopsis else "")
    try:
        res = await provider.chat(
            [ChatMessage(role="system", content=_SYSTEM), ChatMessage(role="user", content=user)],
            preset=DETERMINISTIC)
    except Exception:  # noqa: BLE001 - interpretation is best-effort; fall back deterministically
        return None
    return _parse_tier(res.content or "")


_CAP_SYS = (
    "You label a Linux command-line tool with the capabilities it provides, choosing ONLY from this "
    "fixed list (use the exact names): {vocab}. Given the tool's name and its --help text, reply with a "
    "comma-separated list of the 1-3 best-matching capability names from the list — or 'none' if none "
    "fit. Do not invent capabilities outside the list. Reply with ONLY the names."
)


async def interpret_capabilities(
    provider: ModelProvider, name: str, synopsis: str
) -> list[str]:
    """Map an unmapped tool to capability slugs from the controlled vocabulary, using its --help text.
    Only slugs that exist in CAPABILITY_VOCAB are returned (the model can't invent one); [] on failure.
    Recorded downstream as INFERENCE-source edges, so a curated rule always out-ranks a guess (§8)."""
    system = _CAP_SYS.format(vocab=", ".join(sorted(CAPABILITY_VOCAB)))
    try:
        res = await provider.chat(
            [ChatMessage(role="system", content=system),
             ChatMessage(role="user", content=f"Tool: {name}\n--help:\n{synopsis[:1500]}")],
            options={"temperature": 0.0, "top_k": 40})
    except Exception:  # noqa: BLE001 - interpretation is best-effort
        return []
    answer = (res.content or "").lower()
    return [slug for slug in CAPABILITY_VOCAB if slug in answer][:3]


async def interpret_authority(
    provider: ModelProvider, name: str, *, synopsis: str = ""
) -> tuple[ExecAuthority, str]:
    """(tier, rationale) for a tool. The deterministic classification is the FLOOR: the model may only
    RAISE the tier (more caution), never lower it (§35). Deterministic result stands on any failure."""
    det_tier, det_reason = classify_authority(name)
    model_tier = await _ask_model(provider, name, synopsis)
    if model_tier is None or _SEVERITY[model_tier] <= _SEVERITY[det_tier]:
        return det_tier, det_reason
    return model_tier, f"model raised {det_tier.value} → {model_tier.value} (deterministic floor kept)"
