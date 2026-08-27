import { useQuery } from '@tanstack/react-query'
import { useEffect, type ReactNode } from 'react'

import { api } from '../../lib/api'
import type { GraphNode } from '../../lib/types'
import { useStore } from '../../stores/store'
import { Dot } from '../ui/primitives'

function Field({ k, v }: { k: string; v: ReactNode }): JSX.Element {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-line py-1">
      <span className="panel-label shrink-0">{k}</span>
      <span className="min-w-0 truncate text-right font-mono text-2xs text-dim">{v}</span>
    </div>
  )
}

function confColor(c: number): string {
  return c >= 0.8 ? 'var(--ok)' : c >= 0.5 ? 'var(--important)' : 'var(--bad)'
}

function NodeInspector({ id, label }: { id: string; label?: string }): JSX.Element {
  const { data } = useQuery({ queryKey: ['node', id], queryFn: () => api.graphNode(id, 1) })
  const audit = useQuery({ queryKey: ['audit', label], queryFn: () => api.memoryAudit(label ?? ''), enabled: !!label })
  const node = data?.node as GraphNode | undefined
  const connected = (data?.subgraph.nodes ?? []).filter((n) => n.id !== id)
  const knowledge = (audit.data?.knowledge as Array<Record<string, unknown>>) ?? []

  return (
    <>
      <div className="mb-2 flex items-center gap-2">
        <Dot color="var(--accent)" />
        <span className="text-sm font-medium text-ink">{label ?? node?.name}</span>
        {node && <span className="chip">{node.node_type}</span>}
      </div>
      {node && (
        <div className="mb-3">
          <Field
            k="confidence"
            v={<span style={{ color: confColor(node.confidence) }}>{node.confidence.toFixed(2)}</span>}
          />
          <Field k="source" v={node.source} />
          <Field k="last seen" v={new Date(node.last_seen).toLocaleString()} />
          {Object.entries(node.props ?? {}).slice(0, 6).map(([k, v]) => (
            <Field key={k} k={k} v={String(v)} />
          ))}
        </div>
      )}
      {connected.length > 0 && (
        <div className="mb-3">
          <div className="panel-label mb-1">connected ({connected.length})</div>
          <div className="flex flex-wrap gap-1.5">
            {connected.slice(0, 24).map((n) => (
              <button key={n.id} onClick={() => useStore.getState().select({ kind: 'node', id: n.id, label: n.name })} className="chip hover:border-line-strong">
                {n.name}
              </button>
            ))}
          </div>
        </div>
      )}
      {knowledge.length > 0 && (
        <div>
          <div className="panel-label mb-1">why Sali knows this</div>
          {knowledge.slice(0, 3).map((k, i) => (
            <div key={i} className="mb-1.5 rounded-md border border-line bg-base/40 px-2 py-1.5">
              <div className="text-2xs text-ink">{String(k.content)}</div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                <span className="chip">{String(k.knowledge_type)}</span>
                <span className="chip" title="epistemic status">{String(k.epistemic_status)}</span>
                <span className="chip">{String(k.verification_status)}</span>
                <span className="chip">{Number(k.confidence).toFixed(2)}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  )
}

function EventInspector({ seq }: { seq: string }): JSX.Element {
  const ev = useStore((s) => s.events.find((e) => String(e.seq) === seq))
  if (!ev) return <div className="text-2xs text-faint">event no longer in the buffer.</div>
  return (
    <>
      <div className="mb-2 flex items-center gap-2">
        <span className="font-mono text-sm text-ink">{ev.type}</span>
      </div>
      <Field k="seq" v={ev.seq} />
      <Field k="subject" v={`${ev.subject_type ?? '—'}`} />
      <Field k="at" v={new Date(ev.at).toLocaleString()} />
      <div className="panel-label mb-1 mt-3">payload</div>
      <pre className="overflow-x-auto rounded-md border border-line bg-base/50 p-2 font-mono text-2xs text-dim">
        {JSON.stringify(ev.payload, null, 2)}
      </pre>
    </>
  )
}

function MemoryInspector({ subject }: { subject: string }): JSX.Element {
  const { data } = useQuery({ queryKey: ['audit', subject], queryFn: () => api.memoryAudit(subject) })
  const knowledge = (data?.knowledge as Array<Record<string, unknown>>) ?? []
  return (
    <>
      <div className="mb-2 text-sm text-ink">{subject}</div>
      {knowledge.length === 0 ? (
        <div className="text-2xs text-faint">No memory of “{subject}”.</div>
      ) : (
        knowledge.map((k, i) => (
          <div key={i} className="mb-2 rounded-md border border-line bg-base/40 px-2 py-2">
            <div className="text-xs text-ink">{String(k.content)}</div>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              <span className="chip">{String(k.knowledge_type)}</span>
              <span className="chip">{String(k.epistemic_status)}</span>
              <span className="chip">conf {Number(k.confidence).toFixed(2)}</span>
              {Number(k.contradiction_records) > 0 && (
                <span className="chip" style={{ color: 'var(--bad)' }}>
                  {String(k.contradiction_records)} conflict
                </span>
              )}
            </div>
          </div>
        ))
      )}
    </>
  )
}

export function Inspector(): JSX.Element | null {
  const selection = useStore((s) => s.selection)
  const select = useStore((s) => s.select)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') select(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [select])

  if (!selection) return null
  return (
    <div className="pointer-events-auto absolute right-4 top-20 z-30 max-h-[calc(100%-6rem)] w-[360px] animate-fade-in overflow-y-auto rounded-lg border border-line-strong bg-panel/95 p-3.5 shadow-panel backdrop-blur-md">
      <button
        onClick={() => select(null)}
        className="absolute right-2.5 top-2.5 font-mono text-2xs text-faint hover:text-ink"
      >
        ✕ esc
      </button>
      {selection.kind === 'node' && <NodeInspector id={selection.id} label={selection.label} />}
      {selection.kind === 'event' && <EventInspector seq={selection.id} />}
      {selection.kind === 'memory' && <MemoryInspector subject={selection.label ?? ''} />}
    </div>
  )
}
