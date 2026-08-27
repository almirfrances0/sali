// Wire shapes — these mirror what the Sali backend actually serializes (api/serialize.py, api/stream.py,
// runtime/loop.py LoopEvent). The browser is a client; these are read-only projections of real state.

// ── the durable firehose (/stream and /api/events) ──────────────────────────────────────────────────
export interface SaliEvent {
  seq: number
  id: string
  type: string // e.g. memory.created, task.step_advanced, self.presence, desktop.observed, tool.completed
  subject_type: string | null
  subject_id: string | null
  payload: Record<string, unknown>
  at: string // ISO
}

// ── presence (derived) ──────────────────────────────────────────────────────────────────────────────
export type Presence =
  | 'online'
  | 'idle'
  | 'observing'
  | 'thinking'
  | 'executing'
  | 'waiting'
  | 'learning'
  | 'offline'

export interface PresenceState {
  presence: Presence
  mode: string
  active_run: string | null
  run_state: string | null
}

export interface Health {
  subsystems: Record<string, boolean>
  degraded: string[]
  all_ok: boolean
  internet: boolean
  detail: Record<string, string>
}

// ── the graph (the brain) ───────────────────────────────────────────────────────────────────────────
export interface GraphNode {
  id: string
  node_type: string
  name: string
  canonical_key: string
  props: Record<string, unknown>
  source: string
  confidence: number
  valid_from: string
  valid_until: string | null
  last_verified: string
  last_seen: string
}

export interface GraphEdge {
  id: string
  src_id: string
  dst_id: string
  rel_type: string
  props: Record<string, unknown>
  source: string
  confidence: number
  valid_from: string
}

export interface GraphSnapshot {
  nodes: GraphNode[]
  edges: GraphEdge[]
  collapsed: Record<string, number>
}

// ── the chat turn stream (/ws → LoopEvent) ──────────────────────────────────────────────────────────
export type LoopKind =
  | 'run'
  | 'status'
  | 'token'
  | 'thinking'
  | 'tool'
  | 'retrieval'
  | 'final'
  | 'error'
  | 'confirm'
  | 'sense'

export interface LoopEvent {
  kind: LoopKind
  text: string
  data: Record<string, unknown>
}

export interface ToolCall {
  name: string
  phase: 'start' | 'done'
  ok?: boolean
  summary?: string
  args?: Record<string, unknown>
}

export interface ChatMessage {
  id: string
  role: 'user' | 'sali'
  text: string
  streaming: boolean
  thinking?: string
  tools: ToolCall[]
  runId?: string
  at: number
}

// ── memory inspector ────────────────────────────────────────────────────────────────────────────────
export interface MemoryHitView {
  id: string
  content: string
  layer: string
  source: string
  confidence: number
  knowledge_type: string
  epistemic_status: string
  score: number
  similarity: number | null
  effective_confidence: number
  freshness: number
  stale: boolean
  retriever: string
  scope: string
  valid_from: string
  last_verified: string
  needs_grounding: boolean
}

export interface WorldState {
  focused_app: string | null
  focused_window: string | null
  active_task: string | null
  recent_files: string[]
  recent_commands: { binary: string; ok: boolean | null }[]
  recent_errors: string[]
  gpu: { name: string; vram_total_mib: number; vram_used_mib: number; gpu_util_percent: number; temperature_c: number | null } | null
  memory: { total_mib: number; available_mib: number; used_mib: number } | null
  disk: { total_gib: number; used_gib: number; free_gib: number; percent_used: number } | null
  cpu_pct: number | null
  uptime_s: number | null
  processes: number | null
  listen_ports: number | null
  services: number | null
  containers: number | null
}

export interface Task {
  id: string
  objective: string
  status: string
  steps: { seq: number; description: string; status: string; note: string | null }[]
  result: string | null
  created_at: string
  updated_at: string
}

export interface Schedule {
  id: string
  name: string
  kind: string // 'cron' | 'interval'
  spec: string
  prompt: string
  enabled: boolean
  next_run_at: string | null
  last_run_at: string | null
  last_status: string | null
}

export interface AttentionCounts {
  critical: number
  important: number
  interesting: number
  routine: number | null
  window_hours: number
}

export interface SelfView {
  identity: string
  self_knowledge: string
  environment: Record<string, string>
  mode: string
  current_focus: string | null
  active_operation: string | null
  current_task: string | null
  turns_served: number
  last_success: string | null
  last_failure: string | null
  uncertainty_count: number
  uncertainties: string[]
  learning_queue: string[]
}
