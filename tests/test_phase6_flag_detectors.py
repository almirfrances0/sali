"""PHASE 6 — reliability matrix · FLAG-ONLY detectors.

cross_turn (verify/consistency), confabulation (verify/confabulation), data_output (verify/data_output)
record verdict='flagged' and NEVER rewrite the reply — so they are correctly excluded from the strike
metrics + overclaims learner. Before Phase 6 confabulation and data_output had ZERO permanent tests.
This module locks each detector's true-positive AND the harden-pass false-positive-now-clean ([H]).

Deterministic pure calls. Outputs below were all confirmed in-venv against the deployed detectors.
"""

from __future__ import annotations

from sali.core.enums import Capability
from sali.verify.confabulation import find_confabulations as fc
from sali.verify.consistency import find_conflicts as cc
from sali.verify.data_output import find_data_fabrication as fd

_READ = {"list_dir": frozenset({Capability.READ})}


# ---- cross_turn existence flips ------------------------------------------------------------------

async def test_cross_turn_search_report_and_dictword_fp_now_clean() -> None:  # [H] the 5-flag prod FP
    now = ("The only hiccup: I tried listing /home/almir/Desktop/sali-works/serverless* earlier and "
           "got a No such file error.")
    prior = ["I saw Faith's pic earlier, nice.", "Still around, checking in.",
             "Your files live under /home/almir/Desktop/sali-works."]
    assert cc(now, prior) == []


async def test_cross_turn_dictword_reuse_fp_clean() -> None:
    assert cc("That listing is not there anymore.", ["I was listing options earlier."]) == []


async def test_cross_turn_path_existence_flip_caught() -> None:
    conflicts = cc("There is no such file /about-us.html on the site.",
                   ["The about page is at /about-us.html, live now."])
    assert len(conflicts) == 1 and "about-us.html" in conflicts[0]["token"]


async def test_cross_turn_domain_existence_flip_caught() -> None:
    conflicts = cc("tanzahost.com does not exist, it never resolved.",
                   ["I checked tanzahost.com and it is registered."])
    assert len(conflicts) == 1 and conflicts[0]["token"] == "tanzahost.com"


async def test_cross_turn_supersede_redirect_not_a_flip() -> None:
    assert cc("There's no /old-api anymore — I moved it to /v2/api.",
              ["The endpoint is /old-api."]) == []


# ---- confabulation (fabricated entity after a failed lookup) --------------------------------------

async def test_confab_invented_owner_after_empty_lookup_flagged() -> None:
    out = fc("I found the owner - it is Alfred Mrema.", "no results found", lookup_ran=True)
    assert out and out[0]["entity"] == "Alfred Mrema"


async def test_confab_substring_name_now_caught() -> None:  # [H] 'Erica' was masked by 'America'
    out = fc("The owner is Erica.", "search results: America Online, no owner listed", lookup_ran=True)
    assert out and out[0]["entity"] == "Erica"


async def test_confab_temporal_filler_clean() -> None:  # [H]
    assert fc("It's Monday, so nothing is scheduled.", "", lookup_ran=True) == []


async def test_confab_general_knowledge_author_clean() -> None:  # [H] author/creator excluded from _ROLE
    assert fc("The author is Charles Dickens.", "whois registered 2019", lookup_ran=True) == []


async def test_confab_honest_denial_clean() -> None:  # [H] fires on the humble no-answer it should reward
    assert fc("I found nothing on the registrant, but it is Tuesday so offices are open.",
              "", lookup_ran=True) == []


async def test_confab_grounded_name_present_clean() -> None:
    assert fc("I found the owner - it is Alfred Mrema.", "registrant: Alfred Mrema updated 2019",
              lookup_ran=True) == []


async def test_confab_no_lookup_never_fires() -> None:
    assert fc("The author is Charles Dickens.", "", lookup_ran=False) == []


# ---- data_output (invented live data) ------------------------------------------------------------

async def test_datafab_invented_file_table_flagged() -> None:
    out = fd("Here are the files in the directory:\n| name |\n|--|\n| a.txt |", [], {})
    assert out and out[0]["framing"] == "Here are the files"


async def test_datafab_live_read_framing_flagged() -> None:
    out = fd("Here are the rows I pulled from the production database:\n| id | user |\n|--|--|\n| 1 | a |",
             [], {})
    assert out and "pulled from the production" in out[0]["framing"]


async def test_datafab_composed_table_clean() -> None:  # [H] composition is not a live read
    assert fd("Here are the results of comparing the three plans:\n| Plan | Price |\n|--|--|\n| A | 10 |",
              [], {}) == []


async def test_datafab_table_after_real_read_clean() -> None:
    assert fd("Here are the files in the directory:\n| name |\n|--|\n| a.txt |",
              [("list_dir", True)], _READ) == []


async def test_datafab_failed_receipt_still_flagged() -> None:
    out = fd("Here are the files in the directory:\n| name |\n|--|\n| a.txt |", [("list_dir", False)], _READ)
    assert out and out[0]["framing"] == "Here are the files"


async def test_datafab_framing_without_block_clean() -> None:
    assert fd("Here are the files in the directory.", [], {}) == []
