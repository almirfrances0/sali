import { create } from 'zustand'

import type { ChatMessage, LoopEvent, Presence, PresenceState, SaliEvent, ToolCall } from '../lib/types'

// The one client-side source of truth. Both live surfaces feed it: the durable firehose (/stream) fills
// `events` + presence + brain activation; the chat turn stream (/ws) fills `chat` + the in-flight run.
// Components subscribe to slices, so a single event never re-renders the whole app (§33).

const EVENT_CAP = 600 // bounded ring — old events fall off (the REST backfill has the full history)

export interface BrainActivation {
  at: number
  memoryIds: string[]
  graph: { src: string; rel: string; dst: string; confidence?: number }[]
  query?: string
}

export type ConnState = 'connecting' | 'open' | 'closed'

export interface Selection {
  kind: 'node' | 'memory' | 'event' | 'tool' | 'task'
  id: string
  label?: string
}

interface StoreState {
  // firehose
  events: SaliEvent[]
  lastSeq: number
  streamState: ConnState
  presence: PresenceState | null
  activation: BrainActivation | null
  // chat
  chat: ChatMessage[]
  chatState: ConnState
  sensing: string | null // the live "noticing while you type" hunch
  confirm: { id: string; tool: string; reason: string; args: Record<string, unknown> } | null
  // ui
  selection: Selection | null
  focus: string | null // which region is dominant (null = balanced cockpit)
  toolsOpen: boolean // the tool-catalog overlay (Sali's hands — §20/§21)
  collapsed: Record<string, boolean> // per-widget collapse state
  pinned: string[] // widgets the user chose to keep open (beyond the ones Sali auto-raises on activity)

  // firehose actions
  ingestEvents: (evs: SaliEvent[]) => void
  setStreamState: (s: ConnState) => void
  setPresence: (p: PresenceState) => void
  // chat actions
  applyLoop: (ev: LoopEvent) => void
  pushUser: (text: string) => void
  setChatState: (s: ConnState) => void
  resolveConfirm: () => void
  // ui actions
  select: (s: Selection | null) => void
  setFocus: (f: string | null) => void
  setToolsOpen: (b: boolean) => void
  toggleCollapse: (id: string) => void
  togglePinned: (id: string) => void
}

function newId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36)
}

export const useStore = create<StoreState>((set) => ({
  events: [],
  lastSeq: 0,
  streamState: 'connecting',
  presence: null,
  activation: null,
  chat: [],
  chatState: 'connecting',
  sensing: null,
  confirm: null,
  selection: null,
  focus: null,
  toolsOpen: false,
  collapsed: {},
  pinned: [],

  ingestEvents: (evs) =>
    set((st) => {
      if (evs.length === 0) return st
      const merged = [...st.events, ...evs].slice(-EVENT_CAP)
      const lastSeq = Math.max(st.lastSeq, ...evs.map((e) => e.seq))
      // a self.presence event is a cheap nudge; presence detail is re-fetched by the presence panel
      return { events: merged, lastSeq }
    }),

  setStreamState: (s) => set({ streamState: s }),
  setPresence: (p) => set({ presence: p }),

  pushUser: (text) =>
    set((st) => ({
      chat: [...st.chat, { id: newId(), role: 'user', text, streaming: false, tools: [], at: Date.now() }],
    })),

  applyLoop: (ev) =>
    set((st) => {
      const chat = st.chat.slice()
      // ensure there's a live Sali message to accrete into
      let last = chat[chat.length - 1]
      const ensureSali = (): ChatMessage => {
        if (!last || last.role !== 'sali' || !last.streaming) {
          last = { id: newId(), role: 'sali', text: '', streaming: true, tools: [], at: Date.now() }
          chat.push(last)
        }
        return last
      }
      const patch: Partial<StoreState> = {}
      switch (ev.kind) {
        case 'run': {
          const m = ensureSali()
          m.runId = String(ev.data.run_id ?? '')
          break
        }
        case 'token': {
          const m = ensureSali()
          m.text += ev.text
          break
        }
        case 'thinking': {
          const m = ensureSali()
          m.thinking = (m.thinking ?? '') + ev.text
          break
        }
        case 'tool': {
          const m = ensureSali()
          const d = ev.data as unknown as ToolCall
          if (d.phase === 'start') {
            m.tools = [...m.tools, { name: d.name, phase: 'start', args: d.args }]
          } else {
            const i = m.tools.map((t) => t.name).lastIndexOf(d.name)
            const done: ToolCall = { name: d.name, phase: 'done', ok: d.ok, summary: d.summary }
            if (i >= 0) m.tools[i] = { ...m.tools[i], ...done }
            else m.tools.push(done)
            m.tools = [...m.tools]
          }
          break
        }
        case 'retrieval': {
          patch.activation = {
            at: Date.now(),
            memoryIds: (ev.data.memories as string[]) ?? [],
            graph: (ev.data.graph as BrainActivation['graph']) ?? [],
            query: ev.data.query as string | undefined,
          }
          break
        }
        case 'final': {
          const m = ensureSali()
          if (!m.text.trim() && ev.text) m.text = ev.text
          m.streaming = false
          break
        }
        case 'sense': {
          patch.sensing = ev.text || null
          break
        }
        case 'confirm': {
          patch.confirm = {
            id: String(ev.data.id ?? ''),
            tool: String(ev.data.tool ?? ''),
            reason: ev.text,
            args: (ev.data.args as Record<string, unknown>) ?? {},
          }
          break
        }
        case 'error': {
          const m = ensureSali()
          m.text += (m.text ? '\n\n' : '') + `⚠ ${ev.text}`
          m.streaming = false
          break
        }
      }
      return { chat: [...chat], ...patch }
    }),

  setChatState: (s) => set({ chatState: s }),
  resolveConfirm: () => set({ confirm: null }),
  select: (s) => set({ selection: s }),
  setFocus: (f) => set({ focus: f }),
  setToolsOpen: (b) => set({ toolsOpen: b }),
  toggleCollapse: (id) => set((st) => ({ collapsed: { ...st.collapsed, [id]: !st.collapsed[id] } })),
  togglePinned: (id) =>
    set((st) => ({ pinned: st.pinned.includes(id) ? st.pinned.filter((x) => x !== id) : [...st.pinned, id] })),
}))

export function presenceColor(p: Presence | undefined): string {
  switch (p) {
    case 'executing':
      return 'var(--accent)'
    case 'thinking':
      return 'var(--accent)'
    case 'learning':
      return 'var(--important)'
    case 'waiting':
      return 'var(--important)'
    case 'observing':
      return 'var(--interesting)'
    case 'offline':
      return 'var(--bad)'
    default:
      return 'var(--ok)'
  }
}
