-- 0011_graph_evidence.sql — provenance + multi-source corroboration on graph facts (§6/§48).
-- Every graph node/edge already records ITS source; this records EACH distinct source that has
-- attested it, so a fact corroborated by independent sources (a user statement AND a live
-- observation) can carry higher confidence than a lone claim — and the provenance is auditable.
-- One row per (fact, source): mere re-observation by the same source refreshes the fact (last_seen)
-- without piling up rows.

CREATE TABLE graph_evidence (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  edge_id     uuid REFERENCES graph_edge(id) ON DELETE CASCADE,
  node_id     uuid REFERENCES graph_node(id) ON DELETE CASCADE,
  source      memory_source NOT NULL,
  source_ref  uuid,
  confidence  real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  note        text,
  observed_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_graph_evidence_target CHECK (edge_id IS NOT NULL OR node_id IS NOT NULL));

CREATE INDEX ix_graph_evidence_edge ON graph_evidence (edge_id) WHERE edge_id IS NOT NULL;
CREATE INDEX ix_graph_evidence_node ON graph_evidence (node_id) WHERE node_id IS NOT NULL;
