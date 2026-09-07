"""What Sali says about himself, and what he interrupts Almir for.

Every case here is taken from the real transcript of 2026-09-06/07, not invented:
  "It's taking me a minute since my perception's acting up tonight."   (perception was fine)
  "My perception looks a bit dim tonight, but I'm still fully here."   (also fine)
  "A new service is listening on udp:192.168.1.101:5353."             (that is mDNS)
  "You've been staring at qterminal for almost fifty minutes."        (he was on his phone)
  env:tool:bash:  <- a constraint learned about a shell error fragment
"""

from __future__ import annotations

import pytest

from sali.events.syswatch import _is_worth_noticing
from sali.learning.environment import classify_constraint
from sali.perception import presence
from sali.verify.response_claims import _faculty_complaints

HEALTHY = {"perception": True, "internet": True, "model": True, "embedder": True, "datastore": True}
PERCEPTION_DOWN = {**HEALTHY, "perception": False}


# ── he must not invent a broken faculty to explain himself ───────────────────────────────────────

@pytest.mark.parametrize("sentence", [
    "It's taking me a minute since my perception's acting up tonight.",
    "My perception looks a bit dim tonight, but I'm still fully here.",
    "Sorry, my connection is flaky right now.",
])
def test_an_invented_faculty_complaint_is_struck(sentence: str) -> None:
    assert _faculty_complaints(sentence, HEALTHY), \
        "the health probe says the subsystem is up, so the complaint is not true"


def test_slowness_is_not_treated_as_a_broken_faculty() -> None:
    """Deliberately NOT matched. The local model really is slow, and "this is taking a while" is a
    true thing to say — striking it would make Sali lie in the other direction."""
    assert _faculty_complaints("My model is being slow today.", HEALTHY) == []


def test_a_true_faculty_complaint_is_left_alone() -> None:
    """This must never suppress an honest report — that would be worse than the fabrication."""
    assert _faculty_complaints("My perception is acting up tonight.", PERCEPTION_DOWN) == []


@pytest.mark.parametrize("sentence", [
    "My memory of that is hazy, I could be wrong.",
    "I don't remember exactly what you asked.",
    "Everything's steady here.",
    "My notes on that are thin.",
])
def test_hedging_recall_is_not_a_faculty_complaint(sentence: str) -> None:
    """Saying you might be misremembering is honest. It must not be treated as a broken subsystem."""
    assert _faculty_complaints(sentence, HEALTHY) == []


def test_without_a_health_snapshot_there_is_no_opinion() -> None:
    assert _faculty_complaints("my perception is broken", None) == []


# ── he must not interrupt Almir for the OS breathing ─────────────────────────────────────────────

@pytest.mark.parametrize("proto,addr", [
    ("udp", "192.168.1.101:5353"),                          # mDNS
    ("udp", "[fe80::ead1:ef88:f574:b842]%wlan0:546"),       # DHCPv6 on a link-local address
    ("udp", "0.0.0.0:68"),                                  # DHCP client
    ("udp", "239.255.255.250:1900"),                        # SSDP
    ("tcp", "127.0.0.1:34329"),                             # ephemeral loopback (pre-existing rule)
])
def test_routine_sockets_are_not_announced(proto: str, addr: str) -> None:
    assert _is_worth_noticing(proto, addr) is False


@pytest.mark.parametrize("proto,addr", [
    ("tcp", "0.0.0.0:22"),        # someone could actually reach this
    ("tcp", "0.0.0.0:8080"),
    ("tcp", "192.168.1.101:5432"),
])
def test_a_real_service_is_still_announced(proto: str, addr: str) -> None:
    assert _is_worth_noticing(proto, addr) is True


# ── idle keyboard is not Almir's absence ─────────────────────────────────────────────────────────

def test_presence_describes_the_machine_not_the_man() -> None:
    """Almir messages from his phone. An idle X11 session says nothing about where he is, and Sali
    turned it into "you've been ghosting me for an hour" four times in one evening."""
    line = presence.describe()
    if line is None:
        pytest.skip("no idle signal available on this host")
    lowered = line.lower()
    assert "almir hasn't touched" not in lowered
    assert "almir is at the machine" not in lowered
    assert "keyboard" in lowered or "mouse" in lowered


# ── a shell error fragment is not a program ──────────────────────────────────────────────────────

def test_an_error_fragment_never_becomes_a_machine_constraint() -> None:
    """`binary_of()` returned `bash:` from a malformed command; the shell then said
    "bash: line 1: bash:: command not found", which is true of a thing that was never a program.
    Sali went on to tell Almir he was "retiring the stale belief" about it."""
    assert classify_constraint("bash:", "bash: line 1: bash:: command not found") is None
    assert classify_constraint("--flag", "sh: --flag: command not found") is None
    assert classify_constraint("'quoted", "sh: 'quoted: command not found") is None


def test_real_programs_still_register() -> None:
    assert classify_constraint("ffmpeg", "bash: line 1: ffmpeg: command not found") == "missing"
    assert classify_constraint("/usr/bin/foo", "bash: /usr/bin/foo: No such file or directory") == "missing"
