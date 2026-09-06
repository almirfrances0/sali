from sali.verify.consistency import find_conflicts
from sali.verify.response_claims import validate_response


# --- #5 cross-turn consistency (sync) ---
def test_existence_flip_flagged():
    c = find_conflicts("/about-us.html is a 404 — there is no such page.",
                       ["The About page is at /about-us.html."])
    assert any(x["token"] in ("/about-us.html", "about-us.html", "about-us") for x in c)

def test_no_flip_when_never_asserted():
    assert find_conflicts("There is no /about-us.html.", []) == []

def test_honest_denial_not_a_flip():
    assert find_conflicts("I can't access your bank from here.", ["your bank matters"]) == []


# --- #7 quantity grounding (async) ---
async def _q(text, receipts, rnums):
    rv = await validate_response(text, receipts=receipts, cap_of={}, receipt_numbers=rnums)
    return any(c.kind == "quantity" for c in rv.struck)

async def test_fabricated_count_struck():
    assert await _q("I found 40 files in the folder.", [("list_dir", True)], {3, 5}) is True

async def test_real_count_not_struck():
    assert await _q("I found 40 files.", [("list_dir", True)], {40, 7}) is False

async def test_no_tool_no_strike():
    assert await _q("I found 40 files.", [], set()) is False

async def test_date_not_struck():
    assert await _q("It happened in 2024 last year.", [("list_dir", True)], {5}) is False
