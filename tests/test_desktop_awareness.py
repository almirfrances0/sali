"""Sali watches Almir work — the chain that was silently broken end to end.

Everything here was BUILT and RUNNING and delivering nothing: the perception faculty polled the
focused window every 3 seconds, the aggregator and attention engine and proactive loop were all
correctly wired, and 421 desktop observations were recorded without a single one being about what
Almir had on screen. These tests pin each link, because every one of them failed silently.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sali.events import attention
from sali.events.aggregate import Aggregator
from sali.events.base import DesktopEvent, EventKind as K
from sali.events.engine import _ui_labels, _ui_text
from sali.events.importance import is_noise, score

T0 = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _focus(app: str = "code", title: str = "voice.py", **extra: object) -> DesktopEvent:
    return DesktopEvent(kind=K.WINDOW_FOCUS, target=app, at=T0, extra={"title": title, **extra})


def test_a_window_focus_survives_the_attention_gate() -> None:
    """THE bug: focus scored exactly 0.5 against an INTERESTING threshold of 0.55, so 100% of what
    Almir was doing was discarded before it was ever written. Asserted as a property, never as a
    number, so re-tuning the score cannot silently re-blind him."""
    verdict = attention.assess(importance=score(_focus()))
    assert verdict.action is not attention.AttentionAction.IGNORE


def test_the_window_title_reaches_the_observation() -> None:
    """The app alone says he is in an editor; the title says WHAT he is editing. `window_event` put
    it in `extra` and the aggregator dropped it on the floor."""
    agg = Aggregator()
    ev = _focus()
    agg.add(ev, score(ev))
    obs = agg.flush_all()
    assert len(obs) == 1
    assert obs[0].detail.get("title") == "voice.py"
    assert "voice.py" in obs[0].summary


def test_the_newest_title_wins_inside_one_window() -> None:
    """A person switches documents inside the same app; the one he is on NOW is the one to report."""
    agg = Aggregator()
    for title in ("a.py", "b.py", "c.py"):
        ev = _focus(title=title)
        agg.add(ev, score(ev))
    assert agg.flush_all()[0].detail.get("title") == "c.py"


def test_salis_own_filing_is_not_perception() -> None:
    """344 of 421 observations (82%) were Sali's own task files moving, and the ONLY proactive
    desktop message he ever sent Almir was 'moved 5 files in <uuid>' — himself, as a UUID."""
    mine = DesktopEvent(kind=K.FILE_MOVED, at=T0,
                        target="/home/almir/Desktop/sali-works/tasks/2e7fd9e2/reviews.json")
    his = DesktopEvent(kind=K.FILE_MODIFIED, at=T0, target="/home/almir/Desktop/proj/main.py")
    assert is_noise(mine) is True
    assert is_noise(his) is False


# ── what is actually on screen ───────────────────────────────────────────────────────────────────

TREE = {
    "role": "application", "name": "xfce4-terminal",
    "children": [{
        "role": "frame", "name": "almir@kali: ~",
        "children": [
            {"role": "terminal", "name": "", "text": "def connect_to_databse(host, port):"},
            {"role": "password text", "name": "", "text": ""},   # never populated by the probe
        ],
    }],
}


def test_he_reads_the_text_exactly() -> None:
    """Vision CANNOT do this: a 3440x1440 render of `connect_to_databse` came back silently
    'corrected' to connect_to_database with the model asserting no identifiers were misspelled —
    the same failure on an unscaled crop. Exact text has to come from accessibility, not pixels."""
    assert "connect_to_databse" in _ui_text(TREE)


def test_labels_and_text_are_separate_views() -> None:
    labels = _ui_labels(TREE)
    assert "xfce4-terminal" in labels and "almir@kali: ~" in labels
    assert "connect_to_databse" not in " ".join(labels)   # labels are names, not contents


def test_text_is_deduplicated_and_bounded() -> None:
    """Accessibility trees routinely repeat a string on a container and again on its child, and this
    is written on every window switch into an append-only log."""
    dup = {"role": "document frame", "name": "", "text": "same",
           "children": [{"role": "text", "name": "", "text": "same"}]}
    assert _ui_text(dup) == "same"

    big = {"role": "terminal", "name": "", "text": "x" * 9000}
    assert len(_ui_text(big)) <= 2000


def test_a_document_reads_in_screen_order() -> None:
    """Depth-first, so a terminal buffer reads top to bottom instead of breadth-first scrambled."""
    doc = {"role": "document frame", "name": "", "children": [
        {"role": "text", "name": "", "text": "first"},
        {"role": "text", "name": "", "text": "second"},
        {"role": "text", "name": "", "text": "third"},
    ]}
    assert _ui_text(doc).splitlines() == ["first", "second", "third"]
