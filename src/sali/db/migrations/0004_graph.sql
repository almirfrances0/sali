-- 0004_graph.sql — the temporal knowledge graph + contradiction ledger (Phase 3).
-- Relational nodes+edges with bitemporal validity intervals [valid_from, valid_until);
-- multi-hop reasoning is a recursive CTE (no AGE — decision §E). The digital twin (a later
-- phase) reuses these tables. `contradiction` is the never-overwrite ledger (rules 6/8).

CREATE TABLE graph_node (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  node_type     text NOT NULL,
  name          text NOT NULL,
  canonical_key text NOT NULL,            -- hardware-stable identity (pci:BDF, path:, person:almir…)
  props         jsonb NOT NULL DEFAULT '{}',
  memory_id     uuid REFERENCES memory(id),
  valid_from    timestamptz NOT NULL DEFAULT now(),
  valid_until   timestamptz,
  last_verified timestamptz NOT NULL DEFAULT now(),
  source        memory_source NOT NULL,
  confidence    real NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (valid_until IS NULL OR valid_until >= valid_from));
CREATE UNIQUE INDEX ux_node_current ON graph_node (node_type, canonical_key)
  WHERE valid_until IS NULL;
CREATE INDEX ix_node_type ON graph_node (node_type, name);

CREATE TABLE graph_edge (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  src_id        uuid NOT NULL REFERENCES graph_node(id),
  dst_id        uuid NOT NULL REFERENCES graph_node(id),
  rel_type      text NOT NULL,
  props         jsonb NOT NULL DEFAULT '{}',
  memory_id     uuid REFERENCES memory(id),
  valid_from    timestamptz NOT NULL DEFAULT now(),
  valid_until   timestamptz,
  last_verified timestamptz NOT NULL DEFAULT now(),
  source        memory_source NOT NULL,
  confidence    real NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  superseded_by uuid REFERENCES graph_edge(id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (valid_until IS NULL OR valid_until >= valid_from));
CREATE UNIQUE INDEX ux_edge_current ON graph_edge (src_id, dst_id, rel_type)
  WHERE valid_until IS NULL AND superseded_by IS NULL;
CREATE INDEX ix_edge_src ON graph_edge (src_id, rel_type) WHERE valid_until IS NULL;
CREATE INDEX ix_edge_dst ON graph_edge (dst_id, rel_type) WHERE valid_until IS NULL;
CREATE INDEX ix_edge_valid ON graph_edge (valid_from, valid_until);

-- Never overwrite conflicting history — record it and resolve by evidence priority.
CREATE TABLE contradiction (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subject_type text NOT NULL,                 -- 'memory' | 'graph_edge' | 'graph_node'
  old_id       uuid NOT NULL,
  new_id       uuid,
  status       contradiction_status NOT NULL DEFAULT 'open',
  old_source   memory_source NOT NULL,
  new_source   memory_source NOT NULL,
  old_priority smallint NOT NULL,
  new_priority smallint NOT NULL,
  resolution   text,
  resolved_by  text,
  detail       jsonb NOT NULL DEFAULT '{}',
  created_at   timestamptz NOT NULL DEFAULT now(),
  resolved_at  timestamptz);
CREATE INDEX ix_contradiction_open ON contradiction (status) WHERE status = 'open';
CREATE INDEX ix_contradiction_subject ON contradiction (subject_type, old_id);
