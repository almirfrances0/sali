import { useMemo } from 'react'

import type { SaliEvent } from '../../lib/types'
import { useStore } from '../../stores/store'
import { ago, Dot, Empty, Panel, PanelHeader } from '../ui/primitives'

// Map an event type to a category colour + short kind label — so the stream reads at a glance.
function category(ev: SaliEvent): { color: string; kind: string } {
  const t = ev.type
  if (t === 'desktop.observed') {
    const tier = String(ev.payload.tier ?? 'routine')
    const color =
      tier === 'critical'
        ? 'var(--critical)'
        : tier === 'important'
          ? 'var(--important)'
          : tier === 'interesting'
            ? 'var(--interesting)'
            : 'var(--routine)'
    return { color, kind: 'WORLD' }
  }
  if (t.startsWith('memory.')) return { color: 'var(--interesting)', kind: 'MEMORY' }
  if (t.startsWith('tool.')) return { color: 'var(--accent)', kind: 'TOOL' }
  if (t.startsWith('task.')) return { color: 'var(--important)', kind: 'TASK' }
  if (t.startsWith('learning.')) return { color: '#b39dff', kind: 'LEARN' }
  if (t.startsWith('twin.')) return { color: '#8ab4ff', kind: 'WORLD' }
  if (t.startsWith('conversation.')) return { color: 'var(--accent)', kind: 'CHAT' }
  if (t === 'self.presence') return { color: 'var(--dim)', kind: 'SELF' }
  if (t.startsWith('sali.')) return { color: 'var(--important)', kind: 'SALI' }
  if (t === 'schedule.fired') return { color: 'var(--dim)', kind: 'SCHED' }
  return { color: 'var(--faint)', kind: t.split('.')[0].toUpperCase().slice(0, 5) }
}

const SUMMARY_KEYS = ['summary', 'message', 'title', 'content', 'objective', 'name', 'tool', 'status', 'mode']

export function summarize(ev: SaliEvent): string {
  for (const k of SUMMARY_KEYS) {
    const v = ev.payload[k]
    if (typeof v === 'string' && v.trim()) return v
  }
  return ev.type.split('.').slice(1).join(' ') || ev.subject_type || ev.type
}

function Row({ ev }: { ev: SaliEvent }): JSX.Element {
  const { color, kind } = category(ev)
  const select = useStore((s) => s.select)
  return (
    <button
      onClick={() => select({ kind: 'event', id: String(ev.seq), label: ev.type })}
      className="flex w-full animate-fade-in items-center gap-2.5 rounded px-2 py-1 text-left transition-colors hover:bg-raised/50"
    >
      <span className="w-9 shrink-0 text-right font-mono text-2xs tabular-nums text-faint">{ago(ev.at)}</span>
      <span className="w-1.5 shrink-0">
        <Dot color={color} />
      </span>
      <span className="w-12 shrink-0 font-mono text-[10px] tracking-wider" style={{ color }}>
        {kind}
      </span>
      <span className="truncate text-2xs text-dim">{summarize(ev)}</span>
    </button>
  )
}

export function Activity(): JSX.Element {
  const events = useStore((s) => s.events)
  // newest first; hide the highest-frequency self.presence churn from the human stream (still in the graph)
  const shown = useMemo(() => events.filter((e) => e.type !== 'self.presence').slice(-120).reverse(), [events])

  return (
    <Panel>
      <PanelHeader
        label="Live activity"
        right={<span className="font-mono text-2xs text-faint">{events.length ? `${events.length} live` : 'quiet'}</span>}
      />
      <div className="min-h-0 flex-1 overflow-y-auto px-1.5 py-1.5">
        {shown.length === 0 ? <Empty>Quiet. Sali is watching — events appear as they happen.</Empty> : shown.map((e) => <Row key={`${e.seq}`} ev={e} />)}
      </div>
    </Panel>
  )
}
