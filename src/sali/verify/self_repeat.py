"""Sali quoting himself back as if it were the world.

The most persistent failure of 2026-09-07, and the last one to be fixed. Sali invented a GPU crash
"right after you started playing The Last of Us Part II" at 06:03. At 07:28 — eighty-five minutes and
fifteen messages later — he read his own sentence out of the conversation history and restated it as
shared memory, VERBATIM. Then again at 07:30. Then again at 07:33, with every structural fix already
live. It only stopped when the rows were deleted from the database by hand, and in ordinary use
nobody deletes rows.

The same shape ran through every symptom that day: "Oh wow ... Tell me everything!" survived two
separate fixes; a wedding kept being answered as a bereavement; "she's coming back tomorrow at 5 PM"
was repeated in the sentence immediately after Almir corrected it.

WHY AN INSTRUCTION CANNOT FIX THIS. Assistant turns reach the model as ordinary history, sitting
right next to Almir's real words with nothing to separate them. A retrieved MEMORY carries where it
came from, how confident it is and whether it is unverified; Sali's own past assertion carries
none of that, so his invention outranks his evidence. And by the second turn he is not inventing —
he is quoting a source, and the source is himself. Telling a Q2_K model "don't repeat yourself" was
tried in this codebase and DELETED, because with little signal in the input the strongest instruction
in the tail wins and "hey" got answered with a disk-space report.

So this does not ask. It measures the reply after it exists, and when the reply is substantially the
last one again it is sent back for one more attempt with the repeated words quoted. Deterministic
detection, bounded cost, and nothing the model can talk its way around.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Tuned against the real pairs. The three consecutive GPU replies scored 0.93-1.00 against each
# other; genuinely different replies in the same conversation sat below 0.45. 0.72 sits in the empty
# middle, well clear of both — deliberately NOT lower, because a legitimate answer to a repeated
# question ("what time is it?") SHOULD look like the last one.
REPEAT_THRESHOLD = 0.72
# Below this there is not enough text for a ratio to mean anything: "ok", "yes", "done" are supposed
# to repeat, and two short acknowledgements are not a fault.
_MIN_CHARS = 60

_NORM_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Punctuation, case and spacing removed — the tic survives all three."""
    return _WS_RE.sub(" ", _NORM_RE.sub(" ", (text or "").lower())).strip()


def similarity(new: str, previous: str) -> float:
    a, b = _normalise(new), _normalise(previous)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def is_self_repeat(new: str, previous: str, *, threshold: float = REPEAT_THRESHOLD) -> bool:
    """Is this reply substantially the previous reply again?"""
    if not new or not previous:
        return False
    if len(_normalise(new)) < _MIN_CHARS or len(_normalise(previous)) < _MIN_CHARS:
        return False
    return similarity(new, previous) >= threshold


def repeated_run(new: str, previous: str, *, min_words: int = 6) -> str:
    """The longest stretch of words the two replies share, to quote back.

    Naming the actual text is the one form that lands at this quantisation — the emoji-tic block
    works this way and an abstract rule in the same position did not.
    """
    a, b = _normalise(new).split(), _normalise(previous).split()
    if not a or not b:
        return ""
    match = SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    if match.size < min_words:
        return ""
    return " ".join(a[match.a:match.a + match.size])[:180]
