"""Desktop perception (sali3 Phase 4): the focused app/window (X11) + accessibility tree (AT-SPI)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.perception import _atspi_probe, activewindow, atspi
from sali.perception.base import UiNode, WindowInfo
from sali.perception.fake import FakePerception
from sali.perception.service import DesktopPerception, build_perception
from sali.tools.builtins import perception_tool
from sali.tools.builtins.perception_tool import ActiveWindow
from sali.tools.context import ToolContext


def _ctx(perception: Any = None) -> ToolContext:
    return ToolContext(settings=Settings(), clock=SystemClock(), perception=perception)


def _raise(exc: BaseException) -> Callable[..., Any]:
    def _f(*a: Any, **k: Any) -> Any:
        raise exc
    return _f


def _async_return(value: Any) -> Callable[..., Any]:
    async def _f(*a: Any, **k: Any) -> Any:
        return value
    return _f


class _FakeNode:
    """A duck-typed AT-SPI node. `boobytrap` is a secret exposed via get_text — which _walk must
    NEVER call, so it can never appear in the output (§27: role + name only)."""

    def __init__(self, role: str, name: str, children: Any = (), *, boobytrap: str | None = None) -> None:
        self._role, self._name, self._children = role, name, list(children)
        self._boobytrap = boobytrap

    def get_role_name(self) -> str:
        return self._role

    def get_name(self) -> str:
        return self._name

    def get_child_count(self) -> int:
        return len(self._children)

    def get_child_at_index(self, i: int) -> Any:
        return self._children[i]

    def get_text(self, *a: Any) -> str | None:
        return self._boobytrap


# ── active-window X11 parsing ────────────────────────────────────────────────

def test_wm_class_prefers_the_instance_name() -> None:
    assert activewindow._wm_class('WM_CLASS(STRING) = "qterminal", "QTerminal"') == "qterminal"


def test_pid_and_geometry_parsing() -> None:
    assert activewindow._pid("_NET_WM_PID(CARDINAL) = 4242") == 4242
    geo = activewindow._geometry("X=10\nY=20\nWIDTH=800\nHEIGHT=600\nSCREEN=0")
    assert geo == (10, 20, 800, 600)


def test_read_sync_assembles_window_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.perception.activewindow.shutil.which", lambda name: f"/usr/bin/{name}")
    outputs = {
        ("xdotool", "getactivewindow"): "54525959\n",
        ("xdotool", "getwindowname", "54525959"): "almir@kali: ~\n",
        ("xprop", "-id", "54525959", "WM_CLASS", "_NET_WM_PID"):
            'WM_CLASS(STRING) = "qterminal", "QTerminal"\n_NET_WM_PID(CARDINAL) = 4242\n',
        ("xdotool", "getwindowgeometry", "--shell", "54525959"):
            "WINDOW=54525959\nX=0\nY=0\nWIDTH=1920\nHEIGHT=1080\n",
    }
    monkeypatch.setattr("sali.perception.activewindow._run", lambda argv: outputs.get(tuple(argv)))
    win = activewindow._read_sync()
    assert win == WindowInfo(app="qterminal", title="almir@kali: ~", pid=4242,
                             geometry=(0, 0, 1920, 1080))


def test_read_sync_none_without_xdotool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.perception.activewindow.shutil.which", lambda name: None)
    assert activewindow._read_sync() is None


def test_read_sync_redacts_a_secret_in_the_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.perception.activewindow.shutil.which", lambda name: f"/usr/bin/{name}")
    outputs = {
        ("xdotool", "getactivewindow"): "1\n",
        ("xdotool", "getwindowname", "1"): "root@kali: mysql -password=hunter2\n",
        ("xprop", "-id", "1", "WM_CLASS", "_NET_WM_PID"): 'WM_CLASS(STRING) = "konsole", "Konsole"\n',
    }
    monkeypatch.setattr("sali.perception.activewindow._run", lambda argv: outputs.get(tuple(argv)))
    win = activewindow._read_sync()
    assert win is not None and "hunter2" not in win.title and "[redacted]" in win.title


# ── AT-SPI backend: parsing + redaction + graceful degrade ───────────────────

async def test_ui_tree_parses_and_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    canned = {"ok": True, "truncated": False, "tree": {
        "role": "frame", "name": "Compose",
        "children": [{"role": "text", "name": "my password: hunter2"},
                     {"role": "password text", "name": ""}]}}
    monkeypatch.setattr("sali.perception.atspi._run_probe", lambda py, d, n, pid=0: canned)
    node, detail = await atspi.ui_tree("/usr/bin/python3", max_depth=8, max_nodes=100)
    assert detail == "ok" and node is not None
    assert node.role == "frame"
    # a secret-looking LABEL is scrubbed by the defense-in-depth redact pass
    assert "hunter2" not in node.children[0].name and "[redacted]" in node.children[0].name
    # the password field survives as structure only — no contents were ever emitted
    assert node.children[1].role == "password text" and node.children[1].name == ""


async def test_ui_tree_degrades_when_probe_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.perception.atspi._run_probe", lambda py, d, n, pid=0: None)
    node, detail = await atspi.ui_tree("/usr/bin/python3", max_depth=8, max_nodes=100)
    assert node is None and "probe" in detail


async def test_ui_tree_degrades_when_a11y_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.perception.atspi._run_probe",
                        lambda py, d, n, pid=0: {"ok": False, "detail": "toolkit-accessibility disabled"})
    node, detail = await atspi.ui_tree("/usr/bin/python3", max_depth=8, max_nodes=100)
    assert node is None and "accessibility" in detail


def test_probe_arg_parsing() -> None:
    import sys

    monkey = ["_atspi_probe.py", "--max-depth", "5", "--max-nodes", "40"]
    saved, sys.argv = sys.argv, monkey
    try:
        assert _atspi_probe._arg("--max-depth", 12) == 5
        assert _atspi_probe._arg("--max-nodes", 200) == 40
        assert _atspi_probe._arg("--missing", 7) == 7
    finally:
        sys.argv = saved


# ── service + tool ───────────────────────────────────────────────────────────

def test_build_perception_selects_fake() -> None:
    s = Settings()
    s.perception.backend = "fake"
    assert isinstance(build_perception(s), FakePerception)
    assert isinstance(build_perception(s.perception), FakePerception)


def test_build_perception_defaults_to_desktop() -> None:
    assert isinstance(build_perception(Settings()), DesktopPerception)


async def test_active_window_tool_needs_perception() -> None:
    res = await ActiveWindow().run({}, _ctx(perception=None))
    assert not res.ok and "perception" in (res.error or "")


async def test_active_window_tool_reports_focus() -> None:
    res = await ActiveWindow().run({}, _ctx(perception=FakePerception()))
    assert res.ok
    assert res.output["window"]["app"] == "qterminal"
    assert res.output["ui"] is None  # not requested
    assert "qterminal" in res.display


async def test_active_window_tool_includes_ui_when_asked() -> None:
    res = await ActiveWindow().run({"ui": True}, _ctx(perception=FakePerception()))
    assert res.ok and res.output["ui"]["role"] == "frame"


async def test_active_window_tool_reports_unavailable() -> None:
    class _Dead:
        async def snapshot(self, *, ui: bool = False) -> dict[str, Any]:
            return {"available": False, "window": None, "ui": None, "detail": "no display"}

    res = await ActiveWindow().run({}, _ctx(perception=_Dead()))
    assert not res.ok and "no display" in (res.error or "")


async def test_fake_snapshot_shape() -> None:
    snap = await FakePerception(window=WindowInfo(app="konsole", title="x"),
                                ui=UiNode(role="frame", name="x")).snapshot(ui=True)
    assert snap["available"] and snap["window"]["app"] == "konsole" and snap["ui"]["role"] == "frame"


# ── review fixes: title redaction of CLI-credential forms (§27) ───────────────

def test_read_sync_redacts_inline_cli_password(monkeypatch: pytest.MonkeyPatch) -> None:
    # `mysql -phunter2` (attached flag) is the form the plain keyword regex misses — now covered.
    monkeypatch.setattr("sali.perception.activewindow.shutil.which", lambda name: f"/usr/bin/{name}")
    outputs = {
        ("xdotool", "getactivewindow"): "1\n",
        ("xdotool", "getwindowname", "1"): "mysql -phunter2 sali_test\n",
        ("xprop", "-id", "1", "WM_CLASS", "_NET_WM_PID"): 'WM_CLASS(STRING) = "konsole", "Konsole"\n',
    }
    monkeypatch.setattr("sali.perception.activewindow._run", lambda argv: outputs.get(tuple(argv)))
    win = activewindow._read_sync()
    assert win is not None and "hunter2" not in win.title


# ── review fix: _wm_class must not turn xprop "not found" into a garbage app name ──

def test_wm_class_ignores_not_found_line() -> None:
    assert activewindow._wm_class("WM_CLASS:  not found.") is None


def test_read_sync_falls_back_to_unknown_without_wm_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.perception.activewindow.shutil.which", lambda name: f"/usr/bin/{name}")
    outputs = {
        ("xdotool", "getactivewindow"): "7\n",
        ("xdotool", "getwindowname", "7"): "a menu\n",
        ("xprop", "-id", "7", "WM_CLASS", "_NET_WM_PID"): "WM_CLASS:  not found.\n_NET_WM_PID:  not found.\n",
    }
    monkeypatch.setattr("sali.perception.activewindow._run", lambda argv: outputs.get(tuple(argv)))
    win = activewindow._read_sync()
    assert win is not None and win.app == "unknown" and win.pid is None


# ── review fix: the §27-critical probe walk is now tested with duck-typed nodes ──

def test_walk_emits_role_and_name_only_never_field_contents() -> None:
    secret_field = _FakeNode("password text", "", boobytrap="hunter2")
    root = _FakeNode("frame", "Login", children=[_FakeNode("label", "Password:"), secret_field])
    out = _atspi_probe._walk(root, 5, [100], [False])
    assert out is not None and set(out) <= {"role", "name", "children"}
    for child in out["children"]:  # type: ignore[attr-defined]
        assert set(child) <= {"role", "name", "children"}  # never a 'value'/'text' key
    assert "hunter2" not in json.dumps(out)  # get_text() was never called


def test_walk_flags_truncation_when_children_capped() -> None:
    kids = [_FakeNode("list item", f"row{i}") for i in range(200)]
    elided = [False]
    out = _atspi_probe._walk(_FakeNode("list", "big", children=kids), 5, [500], elided)
    assert out is not None and len(out["children"]) == 128 and elided[0] is True  # type: ignore[arg-type]


def test_walk_respects_the_node_budget() -> None:
    kids = [_FakeNode("item", f"r{i}") for i in range(50)]
    budget = [10]
    out = _atspi_probe._walk(_FakeNode("list", "x", children=kids), 5, budget, [False])
    assert out is not None and budget[0] <= 0 and len(out["children"]) == 9  # type: ignore[arg-type]


def test_walk_degrades_when_a_node_accessor_raises() -> None:
    class _Bad:
        def get_role_name(self) -> str:
            raise RuntimeError("boom")

        def get_name(self) -> str:
            raise RuntimeError("boom")

        def get_child_count(self) -> int:
            raise RuntimeError("boom")

        def get_child_at_index(self, i: int) -> Any:
            raise RuntimeError("boom")

    out = _atspi_probe._walk(_Bad(), 3, [10], [False])
    assert out == {"role": "?", "name": ""}


def test_focused_app_picks_the_active_frame() -> None:
    class _State:
        def __init__(self, active: bool) -> None:
            self._active = active

        def contains(self, st: Any) -> bool:
            return self._active and st == "ACTIVE"

    class _Frame:
        def __init__(self, active: bool) -> None:
            self._s = _State(active)

        def get_state_set(self) -> Any:
            return self._s

    class _Atspi:
        class StateType:
            ACTIVE = "ACTIVE"

    active_app = _FakeNode("application", "focused", children=[_Frame(True)])
    desktop = _FakeNode("desktop", "", children=[_FakeNode("application", "bg", children=[_Frame(False)]),
                                                 active_app])
    assert _atspi_probe._focused_app(desktop, _Atspi) is active_app


# ── review fix: the never-raise contract, exercising the REAL subprocess boundary ──

class _CP:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


async def test_ui_tree_never_raises_on_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    for exc in (subprocess.TimeoutExpired("x", 8), subprocess.CalledProcessError(1, "x"),
                FileNotFoundError("no interpreter")):
        monkeypatch.setattr("sali.perception.atspi.subprocess.run", _raise(exc))
        node, detail = await atspi.ui_tree("/nope", max_depth=4, max_nodes=10)
        assert node is None and detail


async def test_ui_tree_never_raises_on_bad_probe_output(monkeypatch: pytest.MonkeyPatch) -> None:
    for stdout in ("not json{", "[]", "", '{"ok": true, "tree": {"role": "f", "children": 123}}'):
        monkeypatch.setattr("sali.perception.atspi.subprocess.run", lambda *a, s=stdout, **k: _CP(s))
        node, _ = await atspi.ui_tree("/usr/bin/python3", max_depth=4, max_nodes=10)
        assert node is None or node.role == "f"  # malformed 'children' coerced, never raises


def test_read_sync_never_raises_on_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.perception.activewindow.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("sali.perception.activewindow.subprocess.run",
                        _raise(subprocess.TimeoutExpired("x", 5)))
    assert activewindow._read_sync() is None


# ── review fix: the tool's byte-cap anti-flood guard ─────────────────────────

def test_cap_drops_an_oversized_ui_tree() -> None:
    big = {"role": "list", "name": "x",
           "children": [{"role": "item", "name": "y" * 60} for _ in range(500)]}
    snap = {"available": True, "window": {"app": "a"}, "ui": big, "detail": "ok"}
    capped = perception_tool._cap(snap)
    assert capped["ui"] is None and "too large" in capped["detail"]


def test_cap_passes_a_small_ui_tree_through() -> None:
    snap = {"available": True, "window": {}, "ui": {"role": "frame", "name": "x"}, "detail": "ok"}
    assert perception_tool._cap(snap)["ui"] == {"role": "frame", "name": "x"}


# ── review fix: DesktopPerception.snapshot compose/degrade (the production path) ──

async def test_desktop_snapshot_window_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.perception.service.activewindow.active_window",
                        _async_return(WindowInfo(app="konsole", title="x")))
    snap = await DesktopPerception(Settings().perception).snapshot(ui=False)
    assert snap["available"] and snap["window"]["app"] == "konsole"
    assert snap["ui"] is None and snap["detail"] == "window only"


async def test_desktop_snapshot_no_window_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.perception.service.activewindow.active_window", _async_return(None))
    snap = await DesktopPerception(Settings().perception).snapshot(ui=False)
    assert not snap["available"] and "no active window" in snap["detail"]


async def test_desktop_snapshot_window_ok_but_ui_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.perception.service.activewindow.active_window",
                        _async_return(WindowInfo(app="firefox", title="x")))
    monkeypatch.setattr("sali.perception.service.atspi.ui_tree",
                        _async_return((None, "toolkit-accessibility disabled")))
    snap = await DesktopPerception(Settings().perception).snapshot(ui=True)
    assert snap["available"] and snap["ui"] is None and "accessibility" in snap["detail"]
