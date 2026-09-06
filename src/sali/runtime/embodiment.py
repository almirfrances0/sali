"""Sali's embodiment — what his body on THIS machine can and cannot do (§5/§6/§16/§29/§53-capability).

Striking a false capability claim after the fact (verify/response_claims) is a safety net: it fires
only once Sali has already offered to do the impossible. The cure is self-knowledge. Sali has to KNOW
his body — the hands he has here, and just as importantly the ones he does not — so he never offers to
dial a phone he cannot dial. Almir, watching Sali offer to "dial your number next": *"he don't know his
body"*. Exactly. So this states the body as fact, on every turn.

Two halves, both authoritative, both read from here so there is a single source of truth:

* CAN-DO is derived from the LIVE tool registry — never a hardcoded boast. It is the capability groups
  whose tools are actually registered right now, so removing a tool removes the claim automatically.
* CANNOT-DO is the explicit negative registry: whole domains this runtime has NO tool for. It is stated
  as fact rather than left to inference, because "I can't infer a capability I lack" is the very failure
  mode the audit targets. The SAME matcher (`impossible_domain`) is what verify/response_claims uses to
  strike an overreaching claim — so the thing Sali is TOLD he cannot do and the thing that gets STRUCK
  if he says he can are guaranteed to be identical.
"""

from __future__ import annotations

import re
from typing import Any

# Friendly phrase per capability group (groups come from runtime/cognitive._CAPABILITY_GROUPS, derived
# from the live registry). Only groups whose tools are actually registered are ever shown.
_GROUP_FRIENDLY: dict[str, str] = {
    "filesystem": "read & write files",
    "shell": "run shell commands",
    "git": "use git",
    "web_research": "search & fetch the web",
    "archives": "zip & unzip archives",
    "memory": "remember & recall",
    "graph": "keep a knowledge graph",
    "tasks": "run multi-step tasks",
    "planning": "plan & track work",
    "scheduling": "schedule work for later",
    "vision": "see the screen",
    "perception": "watch this desktop",
    "messaging": "send email & notifications",
    "clarification": "ask you a question",
}

# The NEGATIVE registry: (human label, match fragment) for domains this runtime has no tool for. The
# label is what Sali is told he cannot do; the fragment is what a claim of doing it matches. Kept to
# CONCRETE, matchable domains — a vague "physical world" regex would false-match ("go to the folder"),
# so the physical-world limit is stated in prose (CANNOT_DO_PROSE) but not machine-matched.
_CANNOT_DO: tuple[tuple[str, str], ...] = (
    # Tense matters: a fabricated claim is usually PAST ("I called your phone", "I texted her", "I
    # sent you an sms"), while an offer is present/future ("I'll call"). Both must match — the
    # adversarial matrix caught the present-only version letting every "I already did it" through.
    ("make phone calls or send texts",
     r"call(?:ing|ed)?\s+(?:your|his|her|the|my)?\s*(?:phone|number|cell|mobile)"
     r"|phoned?\s+(?:you|him|her|them)|(?:phone|cell|mobile|video)\s+call"
     r"|\bdial(?:ing|ed|led)?\b|r(?:ing|ang)\s+(?:you|him|her|them)"
     r"|(?:send|sending|sent|shoot|shot)\s+(?:you\s+|him\s+|her\s+|them\s+)?"
     r"(?:a\s+|an\s+|the\s+)?(?:text|sms|whatsapp|imessage)"
     r"|text(?:ing|ed)?\s+(?:your|him|her|them)"),
    ("control TVs, lights, or appliances",
     r"control(?:ling)?\s+(?:the\s+)?(?:tv|television|lights?|thermostat|ac|air\s*con|car|door|lock|oven|fridge|heater|speaker)"
     r"|turn(?:ing)?\s+(?:on|off)\s+(?:the\s+)?(?:tv|lights?|thermostat|ac|oven|heater|speaker)"),
    ("touch your bank, cards, or payments",
     r"access(?:ing)?\s+(?:your|his|her|the)?\s*(?:bank|banking|paypal|card|wallet|crypto\s+wallet)"
     r"|(?:log|sign)\s*in(?:to)?\s+(?:your|his|her)\s+(?:bank|paypal|account)"
     r"|(?:make|send)\s+(?:a\s+)?(?:payment|bank\s+transfer)|transfer\s+(?:money|funds)"),
    ("buy, book, or order things",
     r"(?:book(?:ed)?|order(?:ed)?|buy|bought|purchas(?:e|ed)|reserv(?:e|ed))\s+"
     r"(?:you\s+|him\s+|her\s+|them\s+|me\s+)?(?:a\s+|an\s+|the\s+)?"
     r"(?:flight|ticket|uber|taxi|ride|pizza|food|hotel"
     # 'room' is a booking target only in the real world; exclude the UI/layout sense ("reserve a room
     # in the grid/layout/sidebar") that is ordinary front-end vocabulary.
     r"|room(?!\s+(?:in|for|on)\s+(?:the\s+)?(?:layout|grid|sidebar|header|footer|component|div|ui|"
     r"page|dom|css|screen|view|panel|window|form|table))|item"
     r"|it\s+for\s+you|it\s+(?:on|from|online)|online|from\s+(?:amazon|ebay))"),
)

# Prose limits that have no safe regex but Sali should still state as body-facts.
CANNOT_DO_PROSE = "act physically in the world, or perceive anything I have no sensor or tool for"

impossible_domain: re.Pattern[str] = re.compile("|".join(f"(?:{frag})" for _, frag in _CANNOT_DO), re.IGNORECASE)


def can_do_summary(registry: Any) -> str:
    """The live can-do line, derived from the registry (never hardcoded). Empty string on any failure —
    the body line then simply omits the can-do half rather than risk a false boast."""
    try:
        from sali.runtime.cognitive import capability_registry
        groups = capability_registry(registry)
    except Exception:
        return ""
    seen: set[str] = set()
    phrases: list[str] = []
    for g in groups:
        phrase = _GROUP_FRIENDLY.get(g.get("capability", ""), "")
        if phrase and phrase not in seen:
            seen.add(phrase)
            phrases.append(phrase)
    return ", ".join(phrases)


def cannot_do_summary() -> str:
    """The authoritative can't-do line — the negative registry stated as fact."""
    return "; ".join(label for label, _ in _CANNOT_DO) + "; " + CANNOT_DO_PROSE


def render_body(registry: Any) -> str | None:
    """The compact BODY facet for the always-on self-state: what these hands can and cannot do. Stating
    the limits as fact is the point — so Sali never offers a capability he lacks, rather than being
    corrected after he claims it."""
    can = can_do_summary(registry)
    cant = cannot_do_summary()
    if not cant:
        return None
    line = ""
    if can:
        line += f"With my hands on this machine I can {can}. "
    line += (f"What I can't do from here: {cant}. I have no tool for those — so I don't offer them; "
             "I say plainly that I can't.")
    return line


__all__ = ["impossible_domain", "can_do_summary", "cannot_do_summary", "render_body"]
