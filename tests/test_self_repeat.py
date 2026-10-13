"""Sali stops quoting himself back as if it were the world.

The most persistent failure of 2026-09-07. A fabricated GPU crash ("right after you started playing
The Last of Us Part II") was restated VERBATIM at 07:28, 07:30 and 07:33 with every structural fix
already live. Nothing stopped it except deleting the rows by hand, which is not something that
happens in ordinary use.
"""
from __future__ import annotations

from sali.verify.self_repeat import (
    REPEAT_THRESHOLD,
    is_self_repeat,
    repeated_run,
    similarity,
)

A = ("The one about the GPU crashing at 100% load, right after you started playing The Last of Us "
     "Part II? Yeah, that's fresh. It wasn't even a full crash.")
B = ("The one about the GPU crashing at 100% load? Yeah, right after you started playing The Last "
     "of Us Part II. It wasn't even a full crash.")
DIFFERENT = ("Two legs and one hand. That's heavy, Sir. I'm right here though. How are you holding "
             "up? Take your time.")


def test_the_real_repeat_is_caught() -> None:
    """These two are the actual consecutive replies from the transcript."""
    assert similarity(A, B) >= REPEAT_THRESHOLD
    assert is_self_repeat(A, B)


def test_a_genuinely_different_reply_is_not() -> None:
    assert similarity(A, DIFFERENT) < 0.5
    assert not is_self_repeat(A, DIFFERENT)


def test_punctuation_and_case_do_not_hide_it() -> None:
    assert is_self_repeat(A.upper().replace(",", ""), B)


def test_short_acknowledgements_are_allowed_to_repeat() -> None:
    """"ok", "yes", "done" are SUPPOSED to look alike; two of them are not a fault."""
    assert not is_self_repeat("ok", "ok")
    assert not is_self_repeat("Yes, done.", "Yes, done.")


def test_the_repeated_words_can_be_quoted_back() -> None:
    """Naming the actual text is the one form that lands at this quantisation — an abstract rule in
    the same position was tried in this codebase and deleted."""
    run = repeated_run(A, B)
    assert "the last of us part ii" in run


def test_nothing_is_quoted_when_the_overlap_is_trivial() -> None:
    assert repeated_run("completely unrelated words here", "nothing alike at all") == ""


def test_empty_input_is_never_a_repeat() -> None:
    assert not is_self_repeat("", A)
    assert not is_self_repeat(A, "")
    assert similarity("", "") == 0.0
