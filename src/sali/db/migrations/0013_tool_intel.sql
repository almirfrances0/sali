-- 0013_tool_intel.sql — Tool Intelligence substrate (§3/§34/§75).
-- Sali should progressively learn the tools installed on its machine. The REASONING surface for
-- that (machine/tool/capability entities + relationships) lives in the existing knowledge graph
-- (graph_node/graph_edge — node_type is free text, so no enum change), and tool KNOWLEDGE +
-- EXPERIENCES live in the existing memory/learning tables. This migration adds only what has no
-- home yet, and keeps the three concerns §75 asks us to separate in distinct typed tables:
--   * discovered_tool  — the authoritative INVENTORY of external binaries found on the machine
--                        (name/path/package/version), so the runtime can rebuild tool wrappers at
--                        startup and survive the twin's discovery churn. node_id links the graph
--                        node it mirrors; it never becomes a second graph.
--   * tool_authority   — the EXECUTION-AUTHORITY tier (normal/elevated/system_critical, §34/§35)
--                        as an immutable, model-external, temporal record with provenance. It is
--                        never overwritten — a reclassification closes valid_until and inserts a new
--                        current row (same discipline as the rest of the datastore). The tier is
--                        mapped to RiskLevel/Capability in code so the ONE PolicyEngine gate
--                        enforces it (§88/§89) — this table is not a second gate.
-- Both reuse the shared memory_source enum + confidence/provenance conventions from 0001.

CREATE TABLE discovered_tool (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name            text NOT NULL,                    -- the invocable binary name (e.g. 'nmap')
  path            text,                             -- resolved absolute path (shutil.which)
  package         text,                             -- owning OS package, when known (dpkg -S)
  current_version text,
  available       boolean NOT NULL DEFAULT true,    -- flipped false when the binary disappears
  node_id         uuid REFERENCES graph_node(id),   -- the ext_tool graph node this mirrors
  source          memory_source NOT NULL,
  confidence      real NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  structured      jsonb NOT NULL DEFAULT '{}',      -- synopsis, help digest, etc. (later increments)
  first_seen      timestamptz NOT NULL DEFAULT now(),
  last_seen       timestamptz NOT NULL DEFAULT now());
-- One stable row per tool name (upserted on re-discovery), so tool_authority's FK stays valid and
-- the runtime has a single identity per binary.
CREATE UNIQUE INDEX ux_discovered_tool_name ON discovered_tool (name);
CREATE INDEX ix_discovered_tool_available ON discovered_tool (name) WHERE available;

CREATE TABLE tool_authority (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tool_id      uuid NOT NULL REFERENCES discovered_tool(id) ON DELETE CASCADE,
  authority    text NOT NULL CHECK (authority IN ('normal', 'elevated', 'system_critical')),
  rationale    text,                                -- why this tier (auditable)
  classifier   text NOT NULL DEFAULT 'deterministic'
                 CHECK (classifier IN ('deterministic', 'llm_interpret', 'user')),
  capabilities text[] NOT NULL DEFAULT '{}',        -- the security Capability names this tier implies
  source       memory_source NOT NULL,
  confidence   real NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  valid_from   timestamptz NOT NULL DEFAULT now(),
  valid_until  timestamptz,                         -- NULL = the current classification
  created_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (valid_until IS NULL OR valid_until >= valid_from));
-- At most one CURRENT authority per tool; history is retained by closing valid_until (never deleted).
CREATE UNIQUE INDEX ux_tool_authority_current ON tool_authority (tool_id) WHERE valid_until IS NULL;
