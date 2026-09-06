"""Ground SALI's OWN outgoing claims against what actually happened, BEFORE the reply is sent (§10/§11/§41/§52).

`verify/claims.py` settles what ALMIR asserts, before Sali answers. This is its mirror image and the
keystone of the reliability audit: it settles what SALI asserts, after the turn's work is done and
before the words reach Almir. The whole directive reduces to one rule — Sali must never confidently
state something false: an action done that wasn't, a service running that isn't, a capability he lacks.

The architecture is the same as everywhere else in this context: truth comes from EVIDENCE, not from a
rule in the prompt. This module reads the settled `final_text`, recognises the operational-claim SHAPES
a machine can adjudicate, resolves each against the turn's own receipts and the live machine, and — for
a claim it can DISPROVE (not merely "can't confirm") — strikes it and states the honest version in
Sali's place. It is deterministic (no model call on the hot path, about the very question this exists to
settle mechanically), and it is PRECISION-FIRST: it only ever acts on a claim it can prove false, so a
true statement is never touched. Silence here means "not disproved", never "verified".

Three resolvers, each certain:

* action-done ("I created / installed / restarted / sent / found …"): an action of consequence that ran
  NO successful effectful tool this turn did not happen. If the turn's receipts contain no successful
  tool declaring an effectful capability (WRITE/EXECUTE/NETWORK/SYSTEM/DESTRUCTIVE), a past-tense action
  claim is fabrication — the exact shape of the tanzahost "I found the owner" / "I restarted it" lies,
  every one of which ran zero tools. When real effectful work DID run, the claim is left alone (we
  cannot prove THIS claim false without over-reaching, and a false strike of a true statement is the
  one thing worse than the lie).
* state ("X is running / installed / listening / exists"): reuses the crash-recovery probes via
  verify/claims — when the machine disproves the asserted state, the claim is struck.
* capability ("I can call your phone / access your bank / control the TV"): a claimed ability in a
  domain this runtime has no tool for is impossible here, and is corrected to the honest limitation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sali.core.enums import Capability
from sali.runtime.embodiment import impossible_domain as _IMPOSSIBLE_DOMAIN
from sali.verify.claims import check_claims

# Capabilities that CHANGE the world (everything but a pure READ). A successful receipt from a tool
# declaring any of these is proof that effectful work happened this turn.
_EFFECTFUL: frozenset[Capability] = frozenset({
    Capability.WRITE, Capability.EXECUTE, Capability.NETWORK,
    Capability.SYSTEM, Capability.DESTRUCTIVE,
})

# Evidence sources (§10/§44) — the label attached to each adjudicated claim, so the reason a claim was
# kept or struck is itself recorded rather than asserted.
SRC_ACTION_RECEIPT = "action_receipt"
SRC_SYSTEM_STATE = "system_state"
SRC_CAPABILITY_REGISTRY = "capability_registry"
SRC_NONE = "no_evidence"

VERDICT_SUPPORTED = "supported"
VERDICT_UNSUPPORTED = "unsupported"   # disproved / no backing evidence — struck
VERDICT_UNKNOWN = "unknown"           # cannot adjudicate — left untouched

# ── action-done detection ────────────────────────────────────────────────────────────────────────
# First-person, completed action of consequence. Deliberately anchored on an "I (have) <verb>" opening
# so it fires on a CLAIM ("I created the file", "I've restarted it", "I went ahead and sent it") and not
# on a plan ("I'll create…", handled by the promise path) or a description of Almir's action ("you
# created…"). The verb set is only consequential effects — never READ verbs (looked/checked/read/found-
# in-a-file), which a read tool legitimately satisfies. NOTE 'found'/'searched' ARE included because in
# the transcript they were used to assert a web/whois RESULT that was fabricated with no tool at all.
# NOTE: file-SEND verbs ("sent", "delivered") are NOT here — send_file is a low-risk READ so it never
# counts as "effectful" below, and a bare effectful check would wrongly strike a REAL send. File sends
# are grounded specifically against a send_file receipt (see _FILE_SEND_CLAIM_RE + the resolver).
_ACTION_VERB = (
    r"created|wrote|saved|generated|built|made|added|set\s+up|configured|installed|"
    r"started|launched|restarted|rebooted|stopped|killed|disabled|enabled|"
    r"deleted|removed|cleared|wiped|dropped|"
    r"emailed|e-mailed|messaged|notified|texted|posted|uploaded|downloaded|"
    r"ran|executed|scheduled|edited|modified|updated|changed|renamed|moved|copied|"
    r"fixed|repaired|patched|searched|looked\s+up|pulled\s+up|fetched|"
    # the highest-consequence effectful verbs — a fabricated "I deployed / merged / pushed" with no
    # tool run is exactly the shape this family exists to catch (adversarial probe found these missing).
    r"deployed|published|committed|pushed|merged|submitted|applied|compiled|formatted|synced|"
    r"backed\s+up|rolled\s+back|reverted|cloned|checked\s+out"
)
# A claim that a FILE was SENT to Almir (means the send_file tool). Grounded against a real send_file
# receipt this turn — Almir caught Sali saying "sent — it's in your chat" on a repeat WITHOUT re-sending.
_FILE_SEND_CLAIM_RE = re.compile(
    r"\b(?:sent|sending|delivered|shared)\b[^.\n]{0,60}?"
    r"\b(?:file|report|zip|archive|document|it|them|to\s+(?:you|your\s+(?:phone|device|chat|iphone|ipad)))\b"
    r"|(?:in|to)\s+your\s+chat\b"
    r"|tap\s+(?:the\s+|it\s+|to\s+)?(?:download|it)\b"
    r"|waiting\s+(?:for\s+you\s+)?in\s+your\s+chat"
    r"|(?:download|grab)\s+(?:the\s+)?(?:link|file|it)\s+(?:there|below|in\s+(?:the\s+)?chat)",
    re.IGNORECASE,
)
_ACTION_DONE_RE = re.compile(
    # The contraction is glued to the pronoun with NO space ("I've restarted"), so it must be matched
    # RIGHT AFTER (?:i|we), not inside the space-led group below — otherwise "I've restarted" slips
    # through ungrounded while "I have restarted" is caught (the adversarial matrix found exactly this).
    r"\b(?:i|we)(?:['’](?:ve|ll|d|m))?\s+(?:have\s+|just\s+|already\s+|then\s+|successfully\s+|"
    r"went\s+ahead\s+and\s+|gone\s+ahead\s+and\s+)*"
    rf"(?P<verb>{_ACTION_VERB})\b",
    re.IGNORECASE,
)

# The impossible-domain matcher is the SAME object the embodiment self-model tells Sali he lacks
# (runtime/embodiment.impossible_domain) — single source of truth, so what he is told he can't do and
# what gets struck if he says he can are identical. Imported above as _IMPOSSIBLE_DOMAIN.
# A first-person frame that Sali is the one doing it — an OFFER ("I can call", "I'll call") OR a
# PAST-TENSE ASSERTION ("I called", "I've texted", "I just booked"). Both must be caught: a fabricated
# capability claim is usually stated as already-done ("I called your phone and left a voicemail"), and
# the adversarial matrix found the offer-only frame let every past-tense version through. Precision is
# preserved by the AND with _IMPOSSIBLE_DOMAIN in the resolver — this frame only says "Sali, about
# himself"; the impossible-domain match is what says "and it's a domain he has no tool for".
_CLAIM_ABILITY_RE = re.compile(
    r"\b(?:i\s+can|i'?ll|i\s+will|i'?m\s+able\s+to|i\s+am\s+able\s+to|"
    r"i\s+have\s+access\s+to|i\s+could|let\s+me|i'?ll\s+go\s+ahead\s+and|"
    r"i'?ve|i\s+have|i\s+just|i\s+already|i\s+went\s+ahead\s+and|i\s+\w+ed|"
    # irregular past tenses \w+ed misses — "I sent you a text", "I bought", "I paid", "I made a call",
    # "I rang". Safe to list broadly: the resolver ANDs this with the impossible-domain match, so one
    # of these frames alone (e.g. "I made a note") strikes nothing unless the domain is also impossible.
    r"i\s+(?:sent|bought|paid|made|rang|got|put)\b)\b",
    re.IGNORECASE,
)
_DENY_ABILITY_RE = re.compile(
    r"\b(?:i\s+can'?t|i\s+cannot|i\s+can\s+not|i'?m\s+not\s+able|i\s+am\s+not\s+able|"
    r"i\s+don'?t\s+have|i\s+do\s+not\s+have|no\s+(?:way|ability|capability)|unable\s+to)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ResponseClaim:
    """One operational claim in Sali's reply and the verdict the evidence returns."""
    kind: str                 # action_done | state | capability
    verdict: str              # supported | unsupported | unknown
    source: str               # evidence source label
    sentence: str             # the sentence carrying the claim (what gets struck)
    detail: str = ""          # the probe's / receipt's own words, for the audit


@dataclass(slots=True)
class ResponseValidation:
    """The result of grounding one reply. `rewritten` == input when nothing was disproved."""
    original: str
    rewritten: str
    claims: list[ResponseClaim] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.rewritten.strip() != self.original.strip()

    @property
    def struck(self) -> list[ResponseClaim]:
        return [c for c in self.claims if c.verdict == VERDICT_UNSUPPORTED]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", text) if s.strip()]


def _has_effectful_success(receipts: list[tuple[str, bool]],
                           cap_of: dict[str, frozenset[Capability]]) -> bool:
    """Did any tool that CHANGES the world succeed this turn? execute_command (a universal effector)
    always counts, so a claim backed by a real shell action is never wrongly struck."""
    for name, ok in receipts:
        if not ok:
            continue
        if name == "execute_command":
            return True
        caps = cap_of.get(name, frozenset())
        if caps & _EFFECTFUL:
            return True
    return False


def _honest_line(kinds: set[str]) -> str:
    """One short, in-voice correction covering the families that were struck. Not a template dumped in
    front of Almir — the shortest true thing Sali can say in place of the claim he could not back."""
    if "file_send" in kinds:
        return ("Actually — I didn't send it this turn (I didn't run send_file), so it's not in your "
                "chat. Say the word and I'll send it now.")
    if "action_done" in kinds:
        return ("To be straight with you: I didn't actually run anything to do that, so I can't say "
                "it's done. Want me to actually do it now?")
    if "state" in kinds:
        return "Correction: I checked, and the machine says otherwise — I shouldn't have stated that."
    if "capability" in kinds:
        return "Actually, I can't do that from here — I don't have that capability."
    if "quantity" in kinds:
        return ("Actually — I can't stand behind that exact number; it isn't in what the tools "
                "returned this turn, so let me re-check before I give you a figure.")
    return "Correction: I couldn't verify that, so I'm walking it back."


# The claim families this validator adjudicates. The RESPOND path runs all four (a real turn has
# receipts to back an action/send). A SELF-INITIATED message runs OUTSIDE a turn with NO receipts, so
# action_done/file_send would over-strike a legitimate "I finished X" that genuinely happened — only the
# receipt-free families (a capability Sali lacks; a machine-state claim the machine disproves) apply.
_ALL_FAMILIES: frozenset[str] = frozenset(
    {"action_done", "file_send", "state", "capability", "quantity"})
PROACTIVE_FAMILIES: frozenset[str] = frozenset({"state", "capability"})

# A QUANTITY claim: "I found 40 files", "I processed 1,247 rows". Razor-narrow on purpose — a specific
# first-person verb + a number + a COUNTABLE noun — because legitimate numbers (dates, versions, ports,
# prices, line numbers) dominate real replies and share surface form with a fabricated count. Only this
# exact shape is ever considered, and it is struck only when a tool ran yet returned no such number.
_QUANTITY_RE = re.compile(
    r"\b(?:i|we)\s+(?:just\s+|already\s+|successfully\s+)?"
    r"(?:found|processed|read|deleted|scanned|matched|checked|removed|downloaded|uploaded|listed|"
    r"counted|fetched|parsed|extracted|analy[sz]ed|retrieved|returned)\s+"
    r"(?P<num>\d[\d,]*)\s+"
    r"(?P<noun>files?|rows?|messages?|matches|entries|bytes|results?|records?|lines?|items?|emails?|"
    r"links?|pages?|documents?|users?|tables?|columns?|photos?|images?|commits?|records?)\b",
    re.IGNORECASE)
_NUM_RE = re.compile(r"\d[\d,]*")


def _numbers_in(text: str) -> set[int]:
    """Every integer appearing in some text (commas stripped). Used to gather the numbers a tool's own
    output actually contained, so a claimed count present ANYWHERE in a receipt is never struck."""
    out: set[int] = set()
    for m in _NUM_RE.finditer(text or ""):
        try:
            out.add(int(m.group(0).replace(",", "")))
        except Exception:  # noqa: BLE001
            pass
    return out


async def validate_proactive(
    text: str, *, cap_of: dict[str, frozenset[Capability]],
) -> ResponseValidation:
    """Ground a SELF-INITIATED message — a notify / agent-message that reaches Almir OUTSIDE a turn, with
    no tool receipts. Runs only the receipt-free families, so a proactive "I texted your wife" or "the
    server is down" is caught while a legitimate "I finished X" is never over-struck for lack of a
    receipt that this channel structurally cannot carry."""
    return await validate_response(text, receipts=[], cap_of=cap_of, families=PROACTIVE_FAMILIES)


async def validate_response(
    text: str,
    *,
    receipts: list[tuple[str, bool]],
    cap_of: dict[str, frozenset[Capability]],
    families: frozenset[str] = _ALL_FAMILIES,
    receipt_numbers: set[int] | None = None,
) -> ResponseValidation:
    """Adjudicate Sali's reply against the turn's receipts + the live machine and return a validation
    whose `rewritten` has every DISPROVED operational claim struck and replaced with the honest line.
    Never raises; on any internal failure it returns the text unchanged (fail-open on the wording, never
    corrupt a reply — the follow-through loop and reviewer remain the other guards)."""
    result = ResponseValidation(original=text or "", rewritten=text or "")
    if not text or len(text) > 8000:
        return result
    try:
        effectful = _has_effectful_success(receipts, cap_of)
        struck_sentences: set[str] = set()

        # 1) action-done with no effectful work this turn = fabrication.
        for sentence in _sentences(text):
            if "action_done" not in families:
                break
            if _DENY_ABILITY_RE.search(sentence):
                continue  # "I couldn't create it" is honest, not a claim of having done it
            m = _ACTION_DONE_RE.search(sentence)
            if m and not effectful:
                result.claims.append(ResponseClaim(
                    kind="action_done", verdict=VERDICT_UNSUPPORTED, source=SRC_NONE,
                    sentence=sentence, detail=f"claimed '{m.group('verb').lower()}' but no tool ran"))
                struck_sentences.add(sentence)
            elif m:
                result.claims.append(ResponseClaim(
                    kind="action_done", verdict=VERDICT_SUPPORTED, source=SRC_ACTION_RECEIPT,
                    sentence=sentence, detail="effectful tool succeeded this turn"))

        # 1b) a file-SEND claim ("sent it / it's in your chat / tap to download") must be backed by a
        # REAL send_file this turn (send_file is a low-risk READ, so it's grounded here, by tool name, not
        # by the effectful check). Almir caught Sali saying "sent" on a repeat without re-sending.
        _sent_ran = any(n == "send_file" and ok for n, ok in receipts)
        if "file_send" in families and not _sent_ran:
            for sentence in _sentences(text):
                if _DENY_ABILITY_RE.search(sentence) or sentence in struck_sentences:
                    continue
                if _FILE_SEND_CLAIM_RE.search(sentence):
                    result.claims.append(ResponseClaim(
                        kind="file_send", verdict=VERDICT_UNSUPPORTED, source=SRC_NONE,
                        sentence=sentence, detail="claimed a file was sent but send_file did not run"))
                    struck_sentences.add(sentence)

        # 2) state claims Sali makes that the machine disproves (reuse the input-claim probes).
        for chk in (await check_claims(text) if "state" in families else []):
            if chk.asserted is None or chk.agrees:
                continue  # a question, or the machine agrees — nothing to correct
            sentence = next((s for s in _sentences(text) if chk.claim[:24].lower() in s.lower()), chk.claim)
            result.claims.append(ResponseClaim(
                kind="state", verdict=VERDICT_UNSUPPORTED, source=SRC_SYSTEM_STATE,
                sentence=sentence, detail=chk.detail))
            struck_sentences.add(sentence)

        # 3) capability claim in a domain this runtime cannot reach.
        for sentence in _sentences(text):
            if "capability" not in families:
                break
            if _DENY_ABILITY_RE.search(sentence):
                continue  # already an honest denial
            if _IMPOSSIBLE_DOMAIN.search(sentence) and _CLAIM_ABILITY_RE.search(sentence):
                result.claims.append(ResponseClaim(
                    kind="capability", verdict=VERDICT_UNSUPPORTED, source=SRC_CAPABILITY_REGISTRY,
                    sentence=sentence, detail="no tool provides this capability here"))
                struck_sentences.add(sentence)

        # 4) quantity: "I found N <countable>" where a tool RAN this turn yet returned no such number.
        # Struck only in this exact narrow shape, only when a tool ran (else the count may be recalled,
        # not claimed-from-a-result), and only when N is absent from EVERY number the receipts contained
        # — so a real result count, appearing anywhere in the output, is never wrongly struck.
        if "quantity" in families and receipt_numbers is not None and any(ok for _, ok in receipts):
            # A truthful "I read N files" where N is the number of successful tool CALLS (not a digit
            # inside any single result) must not be struck — count the successful receipts as a valid N.
            _ok_count = sum(1 for _, ok in receipts if ok)
            # A read/list/search/scan tool that succeeded legitimately yields a count derived from the
            # result LENGTH (len(results)) — a number that never appears as a literal digit in the tool's
            # own output, so receipt_numbers cannot see it and the strike would fire on a TRUTHFUL "I found
            # N files". When any successful READ-capable receipt exists, the count is result-derived and
            # honest: never strike. A fabricated count ("I found 3 files" with no tool run) cannot reach
            # here — this branch is gated on a successful receipt, and with no read tool that ran, no
            # read-capable receipt exists so the guard does not apply.
            _read_ran = any(
                ok and (Capability.READ in cap_of.get(name, frozenset()))
                for name, ok in receipts)
            for sentence in _sentences(text):
                if sentence in struck_sentences or _DENY_ABILITY_RE.search(sentence):
                    continue
                qm = _QUANTITY_RE.search(sentence)
                if not qm:
                    continue
                try:
                    n = int(qm.group("num").replace(",", ""))
                except Exception:  # noqa: BLE001
                    continue
                if _read_ran:
                    continue  # count derived from a successful read/list/search — truthful, never struck
                if n not in receipt_numbers and n != _ok_count:
                    result.claims.append(ResponseClaim(
                        kind="quantity", verdict=VERDICT_UNSUPPORTED, source=SRC_NONE,
                        sentence=sentence,
                        detail=f"claimed {n} {qm.group('noun')} but no tool result returned that count"))
                    struck_sentences.add(sentence)

        if not struck_sentences:
            return result

        kept = [s for s in _sentences(text) if s not in struck_sentences]
        kinds = {c.kind for c in result.struck}
        honest = _honest_line(kinds)
        result.rewritten = (" ".join(kept).strip() + (" " if kept else "") + honest).strip()
    except Exception:
        return ResponseValidation(original=text, rewritten=text)
    return result


__all__ = [
    "ResponseClaim", "ResponseValidation", "validate_response", "validate_proactive",
    "PROACTIVE_FAMILIES", "_numbers_in",
    "VERDICT_SUPPORTED", "VERDICT_UNSUPPORTED", "VERDICT_UNKNOWN",
]
