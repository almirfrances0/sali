"""Brain-audit Turn 6: seed=42 leak from DETERMINISTIC preset closed.

Every learning/reflection/summarization/interpret call site now passes preset=
explicitly instead of a partial options= dict that inherited DETERMINISTIC's seed=42.

Source-level guards so no future edit re-introduces the leak."""

from __future__ import annotations

import pathlib


def _read(rel: str) -> str:
    return pathlib.Path(rel).read_text()


def test_no_more_partial_opts_constants() -> None:
    """The 5 partial _OPTS constants that leaked seed=42 are all deleted."""
    banned = {
        "src/sali/runtime/loop.py":            "_SUMMARIZE:",
        "src/sali/learning/reflection.py":     "_REFLECT_OPTS:",
        "src/sali/learning/episodes.py":       "_EPISODE_OPTS:",
        "src/sali/learning/procedures.py":     "_NAME_OPTS:",
        "src/sali/twin/interpret.py":          "_OPTS =",
    }
    for path, marker in banned.items():
        src = _read(path)
        assert marker not in src, (
            f"Turn 6 regressed: {marker!r} is back in {path} - would leak seed=42 again")


def test_reflection_uses_creative_preset() -> None:
    """Reflection at temp=0.3 + seed=42 was 'same thought again' every time. Now CREATIVE."""
    src = _read("src/sali/learning/reflection.py")
    assert "preset=CREATIVE" in src, "reflection call should now pass preset=CREATIVE"
    assert "from sali.provider.presets import" in src and "CREATIVE" in src


def test_summarize_and_episodes_use_balanced() -> None:
    """Summaries + episode distillation use BALANCED (mid-temperature, no fixed seed)."""
    for path in ["src/sali/runtime/loop.py", "src/sali/learning/episodes.py",
                 "src/sali/learning/procedures.py"]:
        src = _read(path)
        assert "preset=BALANCED" in src, f"{path} should use preset=BALANCED"


def test_twin_interpret_still_deterministic() -> None:
    """Structured extractor is deliberately DETERMINISTIC - stable JSON output desired."""
    src = _read("src/sali/twin/interpret.py")
    assert "preset=DETERMINISTIC" in src


def test_no_options_dict_leftover_in_learning() -> None:
    """A stray `options={...}` on a learning call would still merge partially over DETERMINISTIC."""
    for path in ["src/sali/learning/reflection.py", "src/sali/learning/episodes.py",
                 "src/sali/learning/procedures.py", "src/sali/twin/interpret.py"]:
        src = _read(path)
        # Check no `options=` argument remains in the call sites we changed.
        assert "options=_" not in src, (
            f"{path} still has an `options=_...` dict passed - would re-leak seed=42")
