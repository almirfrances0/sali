-- 0012_promotion_bar.sql — raise the semantic/preference promotion bar (§57/§20).
-- With the original 0.55 bar even a lone INFERENCE (Sali's own unverified conclusion, conf 0.574)
-- cleared it and became first-class memory. §57 says not every model statement should: nudge the bar
-- just above a lone inference so it lands as a CANDIDATE (needs_grounding) until corroboration lifts
-- it — CONVERSATION (0.611) and every stronger source still clear it immediately, so nothing else
-- changes. §20 applies the same discipline to preferences (never permanent on one weak signal).

UPDATE layer_policy SET promote_min_conf = 0.60 WHERE layer IN ('semantic', 'preference');
