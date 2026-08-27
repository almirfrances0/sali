"""The retrieval router — including the §35 gate: trivial turns retrieve no memory."""

from __future__ import annotations

from sali.retrieval.router import classify


def test_trivial_turns_skip_memory_retrieval() -> None:
    for q in ("hi", "Hello!", "thanks", "ok", "yes", "cool", "good morning",
              "2 + 2", "what is 2+2?", "what's 15 * 3", "calculate 100 / 4"):
        plan = classify(q)
        assert plan.intent == "trivial", q
        assert not plan.use_vector and not plan.use_keyword, q  # no memory search for self-contained turns


def test_real_questions_still_retrieve_memory() -> None:
    for q in ("what database does project x use", "how did we fix docker last time",
              "what did I say about local models", "who owns the VPS"):
        plan = classify(q)
        assert plan.intent != "trivial" and plan.use_vector and plan.use_keyword, q


def test_intents_are_still_classified() -> None:
    assert classify("what's my VRAM right now?").needs_live
    assert classify("what is connected to the VPS?").use_graph
    assert classify("what changed recently?").use_recent
