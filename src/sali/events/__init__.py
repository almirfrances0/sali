"""Continuous desktop perception — the event engine (sali3 Phase 5, §7,11,42).

The on-demand faculties (vision, accessibility) answer "what's on screen right now?" when asked. This
layer makes Sali continuously AWARE: filesystem changes and active-window switches stream in as events,
a DETERMINISTIC filter drops the noise (a build writing 20k files, editor swap files, .git churn), an
importance score ranks what's left, and a burst of related events is AGGREGATED into a single
structured observation. The expensive parts (the LLM, vision) are never in this loop — §41: filter
first, reason later. And observations are only RECORDED, never acted on (§46: never auto-act).
"""
