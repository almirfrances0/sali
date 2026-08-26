"""Deterministic query routing (spec §12)."""

from __future__ import annotations

from sali.retrieval.router import classify


def test_live_state_query_routes_to_inspection() -> None:
    plan = classify("How much RAM is free right now?")
    assert plan.needs_live
    assert plan.intent == "live"


def test_docker_running_query_is_live_not_recalled() -> None:
    # "Docker-not-installed inspected, not recalled" — the router flags it for live inspection.
    plan = classify("Is docker running currently?")
    assert plan.needs_live


def test_relational_query_uses_graph() -> None:
    plan = classify("Which projects are connected to the VPS?")
    assert plan.use_graph
    assert plan.intent == "relational"


def test_temporal_query_pulls_recent() -> None:
    plan = classify("What model was I using last month?")
    assert plan.use_recent
    assert not plan.needs_live


def test_plain_lookup_is_semantic_only() -> None:
    plan = classify("What did I say about local models?")
    assert plan.intent == "lookup"
    assert plan.use_vector and plan.use_keyword
    assert not plan.use_graph and not plan.needs_live
