-- 0029_lease_preemption.sql — cross-process foreground preemption (Prompt 2: live interruption).
-- The foreground lease already serializes execution across processes. To let an INTERRUPT arriving in one
-- process (e.g. the iPhone/API) stop a foreground turn running in ANOTHER process (e.g. the terminal), the
-- interruptor sets a preempt request on the single lease row; the holder's fast poll sees it and cancels
-- its run at the next safe boundary, then releases the lease so the interrupt can acquire it. Additive.

ALTER TABLE execution_lease ADD COLUMN preempt_requested boolean NOT NULL DEFAULT false;
ALTER TABLE execution_lease ADD COLUMN preempt_reason    text;
ALTER TABLE execution_lease ADD COLUMN preempt_by        text;   -- owner_id that requested the preemption
