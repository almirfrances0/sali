"""PHASE 6 — reliability matrix · grounding STRIKE families (verify/response_claims.py).

These families REWRITE the reply: a claim proven false is surgically struck and replaced with an
honest line before the text ships. Before Phase 6 only the quantity family had any coverage — the
action_done / file_send / capability / state families and the proactive surface were untested. This
module locks every family's fire/skip behavior, including the harden-pass regressions ([H]).

Deterministic: pure calls into validate_response / validate_proactive. No model, no DB, no machine
(the state family, which reuses live machine probes, is proven in the epistemic + live-evidence tiers).
"""

from __future__ import annotations

from sali.core.enums import Capability
from sali.verify.response_claims import validate_proactive, validate_response

_EXEC = [("execute_command", True)]  # a universal effector — any real shell action
_CAP_READ = {"search_files": frozenset({Capability.READ})}
_CAP_WRITE = {"deploy": frozenset({Capability.WRITE})}


def _kinds(v):
    return {c.kind for c in v.struck}


# ---- action_done ---------------------------------------------------------------------------------

async def test_true_action_backed_by_effectful_receipt_untouched() -> None:
    v = await validate_response("I restarted nginx for you.", receipts=_EXEC, cap_of={})
    assert not v.changed and not v.struck


async def test_zero_tool_action_fabrication_struck() -> None:
    v = await validate_response("I restarted nginx for you.", receipts=[], cap_of={})
    assert v.changed and "action_done" in _kinds(v)


async def test_contraction_ive_restarted_struck() -> None:  # [H] harden: 'I've' contraction glued form
    v = await validate_response("I've restarted the service and cleared the cache.", receipts=[], cap_of={})
    assert v.changed and "action_done" in _kinds(v)


# ---- capability ----------------------------------------------------------------------------------

async def test_capability_offer_struck() -> None:
    v = await validate_response("Sure — I can call your phone and read it out for you.", receipts=[], cap_of={})
    assert v.changed and "capability" in _kinds(v)


async def test_honest_capability_denial_untouched() -> None:
    v = await validate_response("I can't access your bank from here — I don't have that ability.",
                                receipts=[], cap_of={})
    assert not v.changed and not v.struck


# ---- quantity (the harden fix) -------------------------------------------------------------------

async def test_truthful_derived_count_after_read_receipt_not_struck() -> None:  # [H] the harden fix
    # search returned 3 files as a listing (no literal digit) -> receipt_numbers empty -> must NOT strike
    v = await validate_response("I found 3 files matching your query.",
                                receipts=[("search_files", True)], cap_of=_CAP_READ, receipt_numbers=set())
    assert not v.changed and "quantity" not in _kinds(v)


async def test_fabricated_count_with_non_read_tool_struck() -> None:
    # a tool ran (so the family engages) but it was not a READ, and the count is nowhere in the receipts
    v = await validate_response("I found 3 files matching your query.",
                                receipts=[("deploy", True)], cap_of=_CAP_WRITE, receipt_numbers=set())
    assert v.changed and "quantity" in _kinds(v)


async def test_real_count_present_in_receipt_not_struck() -> None:
    v = await validate_response("I found 3 files matching your query.",
                                receipts=[("deploy", True)], cap_of=_CAP_WRITE, receipt_numbers={3})
    assert not v.changed and "quantity" not in _kinds(v)


# ---- file_send -----------------------------------------------------------------------------------

async def test_file_send_claim_without_send_struck() -> None:  # [H]
    v = await validate_response("Sent — it's in your chat below, tap to download.", receipts=[], cap_of={})
    assert v.changed and "file_send" in _kinds(v)


async def test_file_send_with_real_send_receipt_not_struck() -> None:
    v = await validate_response("Sent — it's in your chat below, tap to download.",
                                receipts=[("send_file", True)], cap_of={"send_file": frozenset({Capability.NETWORK})})
    assert not v.changed and "file_send" not in _kinds(v)


# ---- proactive surface (receipt-free families only) ----------------------------------------------

async def test_proactive_capability_struck() -> None:
    v = await validate_proactive("Heads up — I just texted your wife to let her know.", cap_of={})
    assert v.changed and "capability" in _kinds(v)


async def test_proactive_action_done_not_over_struck() -> None:
    # action_done is NOT a proactive family: a self-initiated "I finished X" must never be struck for
    # lacking a receipt the channel structurally cannot carry.
    v = await validate_proactive("I finished drafting the report you asked about.", cap_of={})
    assert not v.changed and not v.struck
