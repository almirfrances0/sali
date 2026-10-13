#!/usr/bin/env python3
"""Standalone AT-SPI probe — run by the SYSTEM python (which has PyGObject), NOT imported by Sali.

Sali's own venv (3.14) has no PyGObject, but the system python does and the AT-SPI bus is a
system/desktop concern anyway — so the accessibility backend shells out to this script and parses
its JSON, exactly like the vision layer shells out to a screenshot tool.

It prints the accessibility tree of the currently-focused application as JSON: each node's role,
label, and — for text-bearing roles — its CONTENTS, which is what lets Sali notice a wrong command or
a typo rather than merely knowing which app is open.

§27 still holds where it actually protects something: a node of role "password text" is never read,
so a password box appears named "" with no value anywhere in the output. Everything else is scrubbed
by security/redact on the way back (ui_tree redacts the whole tree), masking credential shapes while
leaving prose and code untouched.

Any failure prints ``{"ok": false, "detail": "..."}`` and exits 0 — it must never crash the caller.
"""

from __future__ import annotations

import json
import sys


def _arg(flag: str, default: int) -> int:
    if flag in sys.argv:
        try:
            return int(sys.argv[sys.argv.index(flag) + 1])
        except (ValueError, IndexError):
            return default
    return default


def main() -> None:
    max_depth = _arg("--max-depth", 12)
    max_nodes = _arg("--max-nodes", 200)
    want_pid = _arg("--pid", 0)   # the X11 answer for who is focused; 0 = unknown, fall back to states
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception as exc:  # noqa: BLE001 - report, never crash
        print(json.dumps({"ok": False, "detail": f"AT-SPI binding unavailable: {exc}"}))
        return

    try:
        Atspi.init()
        desktop = Atspi.get_desktop(0)
        app = _focused_app(desktop, Atspi, want_pid)
        if app is None:
            print(json.dumps({"ok": False, "detail": "no focused accessible application "
                              "(is toolkit-accessibility enabled?)"}))
            return
        budget = [max_nodes]
        elided = [False]  # set when a node's children are capped, so `truncated` doesn't under-report
        tree = _walk(app, max_depth, budget, elided)
        print(json.dumps({"ok": True, "tree": tree, "truncated": budget[0] <= 0 or elided[0]}))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "detail": f"AT-SPI query failed: {exc}"}))


# Applications that are always "active" but are never what Almir is looking at: the window manager
# owns a permanently-ACTIVE hidden frame, and the desktop/session shells are furniture.
_NOT_THE_USER = frozenset({"xfwm4", "xfce4-session", "xfsettingsd", "xfdesktop", "xfce4-panel",
                           "marco", "mutter", "kwin", "kwin_x11", "openbox", "gnome-shell"})


def _focused_app(desktop: object, atspi: object, want_pid: int = 0) -> object | None:
    """The application Almir is actually in.

    Selection used to be "first app owning any frame in state ACTIVE", walking the desktop in
    registration order. On this box xfwm4 registers first and owns a permanently-ACTIVE frame, so the
    answer was ALWAYS the window manager — a two-node stub with nothing in it — on every call, while
    the caller reported success. 25 real applications sit on the bus behind that.

    So: trust X11 first. The caller already knows the focused window's PID from xprop, and matching on
    it is exact. Only when no PID is available does this fall back to the ACTIVE-state walk, and even
    then it skips the window manager and the desktop shell rather than letting registration order
    decide.
    """
    active = getattr(atspi, "StateType").ACTIVE  # noqa: B009
    try:
        n = desktop.get_child_count()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None

    apps = []
    for i in range(n):
        try:
            app = desktop.get_child_at_index(i)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            continue
        if app is not None:
            apps.append(app)

    if want_pid:
        for app in apps:
            try:
                if int(app.get_process_id()) == want_pid:  # type: ignore[attr-defined]
                    return app
            except Exception:  # noqa: BLE001
                continue

    fallback = None
    for app in apps:
        try:
            name = str(app.get_name() or "").lower()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            name = ""
        for j in range(_safe_count(app)):
            frame = _safe_child(app, j)
            if frame is None:
                continue
            try:
                if not frame.get_state_set().contains(active):
                    continue
            except Exception:  # noqa: BLE001
                continue
            if name not in _NOT_THE_USER:
                return app
            if fallback is None:
                fallback = app     # the WM, kept only so a bare desktop still answers something
            break
    return fallback


# Roles whose CONTENTS are the point: a terminal's buffer, an editor's document, a text field. A
# label's text is already its name, and a container's "text" is just its children concatenated — so
# reading those would triple the payload and add nothing.
_TEXT_ROLES = frozenset({"terminal", "text", "entry", "paragraph", "document text",
                         "document frame", "document web", "static"})
# Never. Not redacted, not truncated — not read.
_NEVER_READ = frozenset({"password text", "password"})

_TEXT_PER_NODE = 4000
_TEXT_BUDGET = [16000]


def _node_text(node: object, role: str) -> str:
    """The contents of a text-bearing node, or "" — never raising, never for a password field."""
    if role in _NEVER_READ or role not in _TEXT_ROLES or _TEXT_BUDGET[0] <= 0:
        return ""
    try:
        from gi.repository import Atspi

        count = int(Atspi.Text.get_character_count(node))
        if count <= 0:
            return ""
        body = str(Atspi.Text.get_text(node, 0, min(count, _TEXT_PER_NODE)) or "")
    except Exception:  # noqa: BLE001 - no Text interface on this node, or it went away mid-walk
        return ""
    body = body.strip()
    if not body:
        return ""
    body = body[:_TEXT_BUDGET[0]]
    _TEXT_BUDGET[0] -= len(body)
    return body


def _safe_count(node: object) -> int:
    try:
        return int(node.get_child_count())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return 0


def _safe_child(node: object, i: int) -> object | None:
    try:
        return node.get_child_at_index(i)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


def _walk(node: object, depth: int, budget: list[int], elided: list[bool]) -> dict[str, object] | None:
    if node is None or budget[0] <= 0:
        return None
    budget[0] -= 1
    try:
        role = str(node.get_role_name())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        role = "?"
    try:
        name = str(node.get_name() or "")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        name = ""
    out: dict[str, object] = {"role": role, "name": name[:200]}
    body = _node_text(node, role)
    if body:
        out["text"] = body
    children: list[dict[str, object]] = []
    if depth > 0:
        count = _safe_count(node)
        if count > 128:
            elided[0] = True  # this node has more children than we'll emit → the view is incomplete
        for k in range(min(count, 128)):
            if budget[0] <= 0:
                break
            sub = _walk(_safe_child(node, k), depth - 1, budget, elided)
            if sub:
                children.append(sub)
    if children:
        out["children"] = children
    return out


if __name__ == "__main__":
    main()
