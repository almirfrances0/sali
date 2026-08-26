"""The continuous event engine (sali3 Phase 5): noise filter, scoring, aggregation, and the engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sali.events.aggregate import Aggregator
from sali.events.base import DesktopEvent, EventKind, Observation
from sali.events.engine import PerceptionEngine
from sali.events.fswatch import _Handler
from sali.events.importance import is_noise, score

T0 = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)
K = EventKind


def ev(kind: EventKind, target: str, at: datetime = T0) -> DesktopEvent:
    return DesktopEvent(kind=kind, target=target, at=at)


class _Collect:
    def __init__(self) -> None:
        self.seen: list[Observation] = []

    async def observe(self, obs: Observation) -> None:
        self.seen.append(obs)


# ── deterministic noise filter + score ───────────────────────────────────────

def test_is_noise_drops_churn() -> None:
    assert is_noise(ev(K.FILE_MODIFIED, "/proj/.git/index"))
    assert is_noise(ev(K.FILE_MODIFIED, "/proj/__pycache__/x.pyc"))
    assert is_noise(ev(K.FILE_MODIFIED, "/proj/node_modules/lib/a.js"))
    assert is_noise(ev(K.FILE_MODIFIED, "/proj/.venv/x.py"))
    assert is_noise(ev(K.FILE_MODIFIED, "/proj/a.py.swp"))
    assert is_noise(ev(K.FILE_MODIFIED, "/proj/x.tmp"))
    assert is_noise(ev(K.FILE_MODIFIED, "/proj/.#lock"))
    assert is_noise(DesktopEvent(K.WINDOW_FOCUS, "", T0))  # a focus with no app


def test_is_noise_keeps_real_work() -> None:
    assert not is_noise(ev(K.FILE_MODIFIED, "/home/almir/Desktop/proj/src/main.py"))
    assert not is_noise(DesktopEvent(K.WINDOW_FOCUS, "firefox", T0))


def test_score_ranks_meaningfully() -> None:
    assert score(ev(K.FILE_MODIFIED, "/p/src/main.py")) > score(ev(K.FILE_MODIFIED, "/p/data.bin"))
    assert score(ev(K.FILE_DELETED, "/p/x.py")) > score(ev(K.FILE_MODIFIED, "/p/x.py"))
    assert score(DesktopEvent(K.WINDOW_FOCUS, "firefox", T0)) == 0.5
    assert 0.0 <= score(ev(K.FILE_CREATED, "/p/pyproject.toml")) <= 1.0


# ── aggregation ───────────────────────────────────────────────────────────────

def test_aggregator_collapses_a_burst_on_quiet() -> None:
    agg = Aggregator(window_s=2.0, max_count=100_000)
    for i in range(500):
        agg.add(ev(K.FILE_MODIFIED, f"/home/almir/Desktop/proj/src/f{i}.py"), 0.6)
    assert agg.flush_due(T0) == []  # still active, nothing surfaced
    surfaced = agg.flush_due(T0 + timedelta(seconds=2))  # gone quiet
    assert len(surfaced) == 1 and surfaced[0].count == 500
    assert "500 files in src" in surfaced[0].summary


def test_aggregator_flushes_on_max_count() -> None:
    agg = Aggregator(window_s=999.0, max_count=10)
    for i in range(10):
        agg.add(ev(K.FILE_MODIFIED, f"/p/src/f{i}.py"), 0.5)
    surfaced = agg.flush_due(T0)  # not quiet, but the bucket overflowed
    assert len(surfaced) == 1 and surfaced[0].count == 10


def test_single_item_bucket_names_the_file() -> None:
    agg = Aggregator(window_s=1.0)
    agg.add(ev(K.FILE_MODIFIED, "/home/almir/Desktop/proj/notes.md"), 0.6)
    obs = agg.flush_all()
    assert len(obs) == 1 and obs[0].count == 1 and obs[0].summary == "modified notes.md"


def test_window_focus_bucket_names_the_app() -> None:
    agg = Aggregator(window_s=1.0)
    agg.add(DesktopEvent(K.WINDOW_FOCUS, "firefox", T0), 0.5)
    assert agg.flush_all()[0].summary == "focused firefox"


def test_distinct_dirs_are_separate_buckets() -> None:
    agg = Aggregator(window_s=1.0)
    agg.add(ev(K.FILE_MODIFIED, "/p/a/one.py"), 0.5)
    agg.add(ev(K.FILE_MODIFIED, "/p/b/two.py"), 0.5)
    assert len(agg.flush_all()) == 2


def test_single_file_burst_reads_as_changes_not_files() -> None:
    # 20 saves of ONE file must not read as "20 files" (raw-count-vs-distinct bug).
    agg = Aggregator(window_s=1.0)
    for _ in range(20):
        agg.add(ev(K.FILE_MODIFIED, "/p/src/main.py"), 0.5)
    obs = agg.flush_all()[0]
    assert obs.count == 20 and obs.detail["files"] == 1 and obs.summary == "modified main.py (20×)"


def test_active_bucket_flushes_on_age_even_when_never_quiet() -> None:
    # A steady trickle keeps last_at fresh (never "quiet") — the age cap still surfaces it.
    agg = Aggregator(window_s=5.0, max_age_s=10.0, max_count=100_000)
    for k in range(5):
        agg.add(ev(K.FILE_MODIFIED, "/p/src/main.py", T0 + timedelta(seconds=3 * k)), 0.5)
    now = T0 + timedelta(seconds=12)  # last event 0s ago (not quiet) but bucket is 12s old (> 10)
    surfaced = agg.flush_due(now)
    assert len(surfaced) == 1 and surfaced[0].count == 5


# ── engine pipeline ───────────────────────────────────────────────────────────

def test_ingest_filters_noise_but_keeps_work() -> None:
    e = PerceptionEngine()
    assert e.ingest(ev(K.FILE_MODIFIED, "/p/.git/index")) is False
    assert e.ingest(ev(K.FILE_MODIFIED, "/p/src/main.py")) is True


async def test_drain_surfaces_to_sink_and_buffer() -> None:
    sink = _Collect()
    e = PerceptionEngine(sink=sink, aggregate_window_s=2.0)
    e.ingest(ev(K.FILE_MODIFIED, "/p/src/a.py"))
    assert await e.drain(T0) == []  # not quiet
    surfaced = await e.drain(T0 + timedelta(seconds=2))
    assert len(surfaced) == 1 and len(sink.seen) == 1 and len(e.recent()) == 1


async def test_recent_buffer_is_bounded() -> None:
    e = PerceptionEngine(buffer_size=3, aggregate_window_s=0.0)  # window 0 → each drain flushes
    for i in range(10):
        e.ingest(ev(K.FILE_MODIFIED, f"/p/src/f{i}.py"))
        await e.drain(T0)
    assert len(e.recent()) == 3  # deque maxlen holds only the latest


def test_window_event_only_fires_on_change() -> None:
    e = PerceptionEngine()
    snap = {"window": {"app": "firefox", "title": "gmail"}}
    first = e.window_event(snap, T0)
    assert first is not None and first.kind is K.WINDOW_FOCUS and first.target == "firefox"
    assert e.window_event(snap, T0) is None  # unchanged → no event
    changed = e.window_event({"window": {"app": "konsole", "title": "~"}}, T0)
    assert changed is not None and changed.target == "konsole"
    assert e.window_event({"window": {}}, T0) is None  # no app
    assert e.window_event(None, T0) is None


async def test_surface_survives_a_failing_sink() -> None:
    class _Bad:
        async def observe(self, obs: Observation) -> None:
            raise RuntimeError("boom")

    e = PerceptionEngine(sink=_Bad(), aggregate_window_s=0.0)
    e.ingest(ev(K.FILE_MODIFIED, "/p/src/a.py"))
    await e.drain(T0)  # must not raise
    assert len(e.recent()) == 1  # buffered even though the sink threw


def test_emit_sheds_events_under_flood() -> None:
    e = PerceptionEngine(queue_max=2)
    for i in range(5):
        e._emit(ev(K.FILE_MODIFIED, f"/p/src/f{i}.py"))
    assert e.dropped == 3  # 2 queued, 3 shed — visible, never a silent cap


def test_emit_filters_noise_before_the_queue() -> None:
    # §41 filter-first: churn must not consume the bounded queue's budget and evict real events.
    e = PerceptionEngine()
    e._emit(ev(K.FILE_MODIFIED, "/p/.git/index"))
    e._emit(ev(K.FILE_MODIFIED, "/p/__pycache__/x.pyc"))
    assert e._queue.qsize() == 0  # noise dropped before enqueue
    e._emit(ev(K.FILE_MODIFIED, "/p/src/main.py"))
    assert e._queue.qsize() == 1  # real work enqueued


def test_fswatch_handler_maps_events_and_hops_to_the_loop() -> None:
    scheduled: list[DesktopEvent] = []

    class _FakeLoop:
        def call_soon_threadsafe(self, fn: object, arg: DesktopEvent) -> None:
            scheduled.append(arg)  # the watchdog thread hands work to the loop, never touches it directly

    handler = _Handler(_FakeLoop(), lambda e: None)  # type: ignore[arg-type]

    handler.dispatch(SimpleNamespace(is_directory=False, event_type="created",
                                     src_path="/p/a.py", dest_path=""))
    created = scheduled[-1]
    assert created.kind is K.FILE_CREATED and created.target == "/p/a.py"
    handler.dispatch(SimpleNamespace(is_directory=False, event_type="moved",
                                     src_path="/p/a.py", dest_path="/p/b.py"))
    moved = scheduled[-1]
    assert moved.kind is K.FILE_MOVED and moved.target == "/p/b.py"  # uses dest

    n = len(scheduled)
    handler.dispatch(SimpleNamespace(is_directory=True, event_type="created", src_path="/p/d", dest_path=""))
    handler.dispatch(SimpleNamespace(is_directory=False, event_type="closed", src_path="/p/x", dest_path=""))
    assert len(scheduled) == n  # directory churn and unmapped event types are dropped
