"""Referential execution (§10) + channel separation (§14): the deterministic pieces that make
"run it" resolve to the proposed command, and keep background turns off the user's session."""

from __future__ import annotations

from sali.runtime.referential import extract_proposed_command, is_delegation
from sali.runtime.session import background_session_id, persistent_session_id


def test_delegation_phrases_recognised() -> None:
    for yes in (
        "run it", "do it", "execute that", "install it", "go ahead", "proceed",
        "hey i want you to run it", "yes do it", "just run it", "run the command",
        "do what you said", "make it happen", "please go ahead and do it",
    ):
        assert is_delegation(yes), yes
    for no in (
        "what should i install?", "explain the error", "why is it failing",
        "install php on a fresh server means what", "tell me how to run it",  # a question, not a command
        "", "no", "not yet", "hold on",
    ):
        assert not is_delegation(no), no


def test_extract_the_laravel_command() -> None:
    reply = (
        "The error says DOMDocument isn't available — that's PHP's XML extension. On Debian/Kali it's in "
        "php-xml.\n\n```bash\nsudo apt install php-xml\n```\nThen retry your server."
    )
    cmd = extract_proposed_command(reply)
    assert cmd is not None and cmd["command"] == "sudo apt install php-xml"
    assert "php-xml" in cmd["description"]


def test_extract_inline_and_bare_and_none() -> None:
    assert extract_proposed_command("Run `npm install` to pull deps.")["command"] == "npm install"
    assert extract_proposed_command("systemctl restart nginx")["command"] == "systemctl restart nginx"
    # pure prose with no runnable command → nothing (never guess a command out of thin air)
    assert extract_proposed_command("I think the DOM extension is missing; let me look into it.") is None
    assert extract_proposed_command("that looks fine to me") is None


def test_background_session_is_distinct_and_stable() -> None:
    # §14: autonomous/background turns must not share the user's conversation id.
    assert background_session_id() != persistent_session_id()
    assert background_session_id() == background_session_id()  # stable across calls
