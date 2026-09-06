-- Model switcher: persist the owner's chosen active chat model so it survives restarts.
-- sali_state is the single-row (id=true) runtime-state table; one nullable column is all we need.
-- NULL = use the configured default (settings.model.chat_model). The provider reads this at startup
-- and the POST /model endpoint updates it. Still exactly one active model — the owner just picks it.

ALTER TABLE sali.sali_state
    ADD COLUMN IF NOT EXISTS active_chat_model TEXT;

COMMENT ON COLUMN sali.sali_state.active_chat_model IS
    'Owner-selected active chat model (model switcher); NULL = configured default.';
