"""PHASE 6 — reliability matrix · verbatim anchoring & voice register.

Q2_K garbles rare exact tokens (tanzahost->tanzhost, sali-works->soli-works); anchor_args corrects a
tool arg back toward the token the user actually typed, WITHOUT touching ordinary dictionary words. The
apology-tic nudge (_apology_opener) curbs the grovel opener — harden-fixed to catch the lead-in forms
('I apologize', 'My apologies') and to leave empathy ('Sorry to hear') alone. All deterministic.
"""

from __future__ import annotations

from sali.context.engine import _apology_opener
from sali.runtime.verbatim import anchor_args, distinctive_tokens

_WS = "/home/almir/Desktop/sali-works"


# ---- verbatim anchoring corrects garbles, spares real words --------------------------------------

def test_garbled_domain_corrected_toward_user_anchor() -> None:
    anchors = distinctive_tokens("please search tanzahost.com for me")
    fixed, corrections = anchor_args({"query": "tanzhost.com"}, anchors, workspace=_WS)
    assert fixed["query"] == "tanzahost.com"
    assert corrections and corrections[0]["to"] == "tanzahost.com"


def test_ordinary_dictionary_word_not_miscorrected() -> None:
    # 'desert' must NOT be pulled toward 'dessert' from the user's message — both are real words.
    anchors = distinctive_tokens("I love the dessert here")
    fixed, corrections = anchor_args({"query": "desert"}, anchors, workspace=_WS)
    assert fixed["query"] == "desert" and corrections == []


def test_correct_token_left_untouched() -> None:
    anchors = distinctive_tokens("open tanzahost.com")
    fixed, corrections = anchor_args({"query": "tanzahost.com"}, anchors, workspace=_WS)
    assert fixed["query"] == "tanzahost.com" and corrections == []


# ---- apology-tic nudge ---------------------------------------------------------------------------

def test_apology_openers_caught_including_lead_in_forms() -> None:  # [H]
    for s in ("I apologize for that.", "My apologies for the mix-up.", "I'm sorry, my mistake.",
              "Ugh, sorry, my bad.", "sorry, my bad."):
        assert _apology_opener(s), s


def test_empathy_and_clean_openers_not_nudged() -> None:
    assert not _apology_opener("Sorry to hear that, here is the fix.")   # empathy, not self-blame
    assert not _apology_opener("Sorted it out, all good now.")          # no apology
    assert not _apology_opener("Here is the deploy log you asked for.")  # clean turn -> zero tokens
