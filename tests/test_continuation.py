"""The continuation packet (§11-12): the active task + current step + failed steps survive a fold/
compaction DETERMINISTICALLY (not via model prose), and the model's labeled notes parse into fields."""

from __future__ import annotations

from types import SimpleNamespace

from sali.runtime.continuation import (
    build_packet,
    parse_sections,
    render_packet,
    render_task_header,
    task_fields,
)


def _step(seq: int, desc: str, status: str = "pending", **kw: object) -> SimpleNamespace:
    return SimpleNamespace(
        seq=seq, description=desc, status=status,
        last_error=kw.get("last_error"), failure_class=kw.get("failure_class"), attempts=kw.get("attempts", 0),
    )


def _task() -> SimpleNamespace:
    steps = [
        _step(1, "build", "done"),
        _step(2, "start service", "failed", last_error="bind: permission denied", failure_class="permission", attempts=2),
        _step(3, "verify", "pending"),
    ]
    nxt = next(s for s in steps if s.status in ("pending", "running"))
    return SimpleNamespace(objective="Deploy the site", status="running", steps=steps, next_step=nxt)


def test_parse_sections_is_tolerant() -> None:
    text = (
        "DONE: built the app\n"
        "NEXT: start the service\n"
        "FACTS: nginx installed; port 80 chosen\n"
        "  (this line wraps the FACTS field)\n"
        "OPEN: does external HTTPS work?\n"
        "GARBAGE: ignore me"  # not a known section → dropped
    )
    p = parse_sections(text)
    assert p["done"] == "built the app"
    assert p["next"] == "start the service"
    assert "wraps the FACTS field" in p["facts"]
    assert "HTTPS" in p["open"]
    assert "garbage" not in p


def test_task_fields_are_deterministic_and_include_failures() -> None:
    tf = task_fields(_task())
    assert tf["objective"] == "Deploy the site" and tf["status"] == "running"
    assert tf["next"] == {"seq": 3, "description": "verify"}
    assert len(tf["step_failures"]) == 1
    f = tf["step_failures"][0]
    assert f["seq"] == 2 and f["class"] == "permission" and f["attempts"] == 2
    assert task_fields(None) == {}


def test_render_task_header_survives_prose_loss() -> None:
    header = render_task_header(_task())
    assert "TASK: Deploy the site" in header
    assert "1.✓" in header and "2.✗" in header and "3.·" in header  # step ticks
    assert "NEXT: step 3 — verify" in header
    assert "FAILED step 2 (permission" in header and "permission denied" in header
    assert render_task_header(None) == ""


def test_packet_roundtrips_and_renders() -> None:
    packet = build_packet(_task(), "DONE: built it\nDECISIONS: chose nginx\nOPEN: HTTPS?")
    assert packet["task"]["objective"] == "Deploy the site"
    assert packet["notes"]["decisions"] == "chose nginx"
    rendered = render_packet(packet)
    # deterministic task state + the parsed notes both come back
    assert "TASK: Deploy the site" in rendered
    assert "NEXT: step 3 — verify" in rendered  # from the task, not the (absent) model NEXT
    assert "DECISIONS: chose nginx" in rendered
    assert "OPEN: HTTPS?" in rendered
    assert "FAILED step 2" in rendered  # failures carried through the jsonb round-trip
    # a task-less packet still renders its notes
    assert "DECISIONS: x" in render_packet(build_packet(None, "DECISIONS: x"))
    assert render_packet(None) == "" and render_packet("not a dict") == ""
