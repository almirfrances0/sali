#!/usr/bin/env python3
"""Standalone AT-SPI probe — run by the SYSTEM python (which has PyGObject), NOT imported by Sali.

Sali's own venv (3.14) has no PyGObject, but the system python does and the AT-SPI bus is a
system/desktop concern anyway — so the accessibility backend shells out to this script and parses
its JSON, exactly like the vision layer shells out to a screenshot tool.

It prints the accessibility tree of the currently-focused application as JSON: each node's role and
label only. It NEVER reads a field's text contents (§27: never surface a secret) — so a password box
appears as a node named "" of role "password text", and its value never exists in the output at all.
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
        app = _focused_app(desktop, Atspi)
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


def _focused_app(desktop: object, atspi: object) -> object | None:
    active = getattr(atspi, "StateType").ACTIVE  # noqa: B009
    try:
        n = desktop.get_child_count()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None
    for i in range(n):
        try:
            app = desktop.get_child_at_index(i)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            continue
        if app is None:
            continue
        for j in range(_safe_count(app)):
            frame = _safe_child(app, j)
            if frame is None:
                continue
            try:
                if frame.get_state_set().contains(active):
                    return app
            except Exception:  # noqa: BLE001
                continue
    return None


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
