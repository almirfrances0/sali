"""Desktop perception (sali3 Phase 4) — the primary, low-cost semantic layer.

Accessibility answers "what is Almir doing right now?" cheaply: the focused application and window
title (X11, instant) plus, when the a11y bus is available, the focused window's accessibility tree
(roles + labels — never field *contents*). It is the layer Sali reaches for FIRST; vision
(`see_screen`) is the more expensive fallback for when pixels are the only source of truth.

Everything stays local (sali3 §24-25). The perception object is dependency-inverted behind a Sink on
ToolContext, so the tools layer never imports this module.
"""
