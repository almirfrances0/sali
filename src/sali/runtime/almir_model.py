"""Almir's state model — tracks his emotional/communicative state from recent interactions.

Sali is a digital person who lives with Almir. Understanding his state — what he's working on,
how he's feeling, how he communicates — is fundamental to being a good companion, not just a
responsive tool. This module reads recent messages and derives a compact state that the context
engine injects into every turn.

This is NOT sentiment analysis. It's pattern recognition: communication frequency, topic focus,
emotional signals, and interaction style. Sali uses this to adjust how she speaks, when she
initiates conversation, and what she pays attention to.
"""

from __future__ import annotations

from typing import Any

# Emotional signal patterns — deterministic, not model-dependent.
_HAPPY_SIGNALS = ("amazing", "awesome", "love", "great", "perfect", "excellent", "fantastic",
                  "beautiful", "wonderful", "incredible", "brilliant", "nice", "cool", "fun",
                  "excited", "happy", "glad", "enjoy", "celebrate", "congratulations")
_FRUSTRATED_SIGNALS = ("frustrated", "annoyed", "angry", "broken", "doesn't work", "doesn't help",
                       "not working", "failed", "wrong", "terrible", "awful", "hate", "stupid",
                       "useless", "waste", "why can't", "why doesn't", "fix this", "still not",
                       "again", "keeps", "keeps failing")
_FOCUSED_SIGNALS = ("building", "coding", "working on", "developing", "implementing", "writing",
                    "designing", "creating", "debugging", "fixing", "deploying", "testing")
_TIRED_SIGNALS = ("tired", "exhausted", "sleepy", "going to bed", "goodnight", "good night",
                  "need to rest", "taking a break", "brb", "be back")


def derive_almir_state(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive Almir's current state from recent messages.

    Returns a compact dict with:
    - tone: happy/frustrated/focused/tired/neutral
    - communication_style: brief/detailed/emoji-heavy/minimal
    - recent_topics: list of topics from recent messages
    - last_emotion: the most recent emotional signal
    """
    if not messages:
        return {"tone": "neutral", "style": "unknown", "topics": [], "last_emotion": None}

    # Analyze the last 10 user messages
    user_msgs = [m for m in messages if m.get("role") == "user"][-10:]
    if not user_msgs:
        return {"tone": "neutral", "style": "unknown", "topics": [], "last_emotion": None}

    # Tone detection
    happy_count = 0
    frustrated_count = 0
    focused_count = 0
    tired_count = 0
    last_emotion = None

    for msg in user_msgs:
        text = (msg.get("content") or "").lower()
        if any(s in text for s in _HAPPY_SIGNALS):
            happy_count += 1
            last_emotion = "happy"
        if any(s in text for s in _FRUSTRATED_SIGNALS):
            frustrated_count += 1
            last_emotion = "frustrated"
        if any(s in text for s in _FOCUSED_SIGNALS):
            focused_count += 1
            last_emotion = "focused"
        if any(s in text for s in _TIRED_SIGNALS):
            tired_count += 1
            last_emotion = "tired"

    # Determine dominant tone
    if frustrated_count >= 2:
        tone = "frustrated"
    elif happy_count >= 2:
        tone = "happy"
    elif focused_count >= 2:
        tone = "focused"
    elif tired_count >= 1:
        tone = "tired"
    else:
        tone = "neutral"

    # Communication style
    avg_len = sum(len(m.get("content") or "") for m in user_msgs) / max(len(user_msgs), 1)
    emoji_count = sum(1 for m in user_msgs if any(c in (m.get("content") or "") for c in "😂😊🤔👍❤️🔥"))
    if avg_len < 20:
        style = "brief"
    elif avg_len > 200:
        style = "detailed"
    elif emoji_count > len(user_msgs) // 2:
        style = "emoji-heavy"
    else:
        style = "normal"

    return {
        "tone": tone,
        "style": style,
        "topics": [],  # topic extraction would need NLP, kept simple
        "last_emotion": last_emotion,
    }
