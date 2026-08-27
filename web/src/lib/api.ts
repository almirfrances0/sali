// Typed REST client for Sali's snapshot endpoints. Same-origin in production (served by FastAPI); in dev
// Vite proxies /api to the running `sali serve`. These load initial/paged state; the live firehose comes
// over /stream and the chat over /ws.
import type {
  AttentionCounts,
  GraphSnapshot,
  Health,
  MemoryHitView,
  PresenceState,
  SaliEvent,
  SelfView,
  Task,
  WorldState,
} from './types'

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { accept: 'application/json' } })
  if (!res.ok) throw new Error(`${path} → ${res.status}`)
  return (await res.json()) as T
}

export const api = {
  health: () => get<Health>('/api/health'),
  presence: () => get<PresenceState>('/api/presence'),
  self: () => get<SelfView>('/api/self'),
  world: () => get<WorldState>('/api/world'),
  attention: (hours = 24) => get<AttentionCounts>(`/api/attention/counts?hours=${hours}`),
  graphSnapshot: (limit = 220) => get<GraphSnapshot>(`/api/graph/snapshot?limit=${limit}`),
  graphNode: (id: string, depth = 1) =>
    get<{ node: unknown; subgraph: GraphSnapshot }>(`/api/graph/node/${id}?depth=${depth}`),
  neighborhood: (id: string, depth = 1, limit = 80) =>
    get<GraphSnapshot>(`/api/graph/neighborhood/${id}?depth=${depth}&limit=${limit}`),
  memorySearch: (q: string, k = 8) =>
    get<{ query: string; hits: MemoryHitView[] }>(`/api/memory/search?q=${encodeURIComponent(q)}&k=${k}`),
  memoryAudit: (subject: string) =>
    get<{ subject: string; found: boolean; knowledge: unknown[]; connected: unknown }>(
      `/api/memory/audit?subject=${encodeURIComponent(subject)}`,
    ),
  events: (since = 0, limit = 200, type?: string) =>
    get<{ events: SaliEvent[]; since: number }>(
      `/api/events?since=${since}&limit=${limit}${type ? `&type=${encodeURIComponent(type)}` : ''}`,
    ),
  tasks: () => get<{ open: Task[]; current: Task | null }>('/api/tasks'),
  schedules: () => get<{ schedules: unknown[] }>('/api/schedules'),
  learningQueue: () => get<{ pending: unknown[]; count: number }>('/api/learning/queue'),
  tools: () =>
    get<{ tools: { name: string; description: string; risk: number; capabilities: string[]; idempotent: boolean }[] }>(
      '/api/tools',
    ),
  toolExecutions: (limit = 25) => get<{ executions: unknown[] }>(`/api/tools/executions?limit=${limit}`),
  runs: (limit = 20) => get<{ runs: unknown[] }>(`/api/runs?limit=${limit}`),
  runEvents: (runId: string) => get<{ run_id: string; events: unknown[] }>(`/api/runs/${runId}/events`),
}
