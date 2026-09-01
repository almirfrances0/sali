-- 0042_conversations.sql — Behavior evolution, personal development & human-like ongoing life.
-- Additive + minimal (§40: extend, do not duplicate). Behavioral tendencies already exist
-- (behavior_proposal + BehaviorStore + classify_feedback, 0034), people exist (person, 0039), the
-- activity ledger already supports a per-activity 'waiting_for_user' status (0036) so waiting is a
-- property of an ACTIVITY, not the whole runtime (§16), and proactive messaging + memory provenance +
-- contradiction handling already exist. The message-log `conversation` table (0001) is a different
-- concept (raw turns), so this uses `conversation_thread` for the async INTERACTION-thread identity.
-- This adds only what's missing: durable CONVERSATION-THREAD + PENDING-QUESTION identity so a question
-- survives compaction/restart, blocks only the activity that depends on it, and a late free-text answer
-- can be routed back to the right question days later (§3/§4/§23). It COMPLEMENTS task_question (the
-- reviewer-gated task-clarification path, 0033) rather than replacing it.

-- ── Conversation threads: an interaction thread that survives, pauses, and resumes (§7) ──────────────
CREATE TABLE conversation_thread (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id       uuid,                      -- soft ref to person
  person_name     text,
  topic           text,
  status          text NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'paused', 'completed')),
  last_message_at timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_conversation_thread_open ON conversation_thread (status) WHERE status IN ('open', 'paused');

-- ── Pending questions: durable conversational identity, dependency-aware waiting (§4/§16/§24) ────────
CREATE TABLE pending_question (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id       uuid REFERENCES conversation_thread(id) ON DELETE SET NULL,
  person_id       uuid,
  person_name     text,
  task_id         uuid,                      -- soft refs (the task/activity may be archived/cleaned)
  activity_id     uuid,
  question        text NOT NULL,
  why_it_matters  text,
  options         jsonb NOT NULL DEFAULT '[]',
  dependency_kind text NOT NULL DEFAULT 'blocking'
                  CHECK (dependency_kind IN ('blocking', 'advisory', 'optional')),
  status          text NOT NULL DEFAULT 'waiting'
                  CHECK (status IN ('waiting', 'answered', 'superseded', 'cancelled', 'expired',
                                    'no_longer_needed')),
  answer          text,
  asked_count     int NOT NULL DEFAULT 1,     -- how many times Sali surfaced it (§14 anti-nag)
  last_asked      timestamptz NOT NULL DEFAULT now(),
  next_followup   timestamptz,
  expires_at      timestamptz,
  asked_at        timestamptz NOT NULL DEFAULT now(),
  answered_at     timestamptz);
CREATE INDEX ix_pending_question_waiting ON pending_question (status) WHERE status = 'waiting';
CREATE INDEX ix_pending_question_thread ON pending_question (thread_id);
CREATE INDEX ix_pending_question_person ON pending_question (person_name) WHERE status = 'waiting';
