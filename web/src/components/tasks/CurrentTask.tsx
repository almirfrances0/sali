import { useQuery } from '@tanstack/react-query'

import { api } from '../../lib/api'
import { ago, Panel } from '../ui/primitives'

function Row({ k, v }: { k: string; v: React.ReactNode }): JSX.Element {
  return (
    <div className="flex items-center justify-between border-t border-line/50 py-1.5">
      <span className="text-2xs text-dim">{k}</span>
      <span className="font-mono text-2xs tabular-nums text-ink">{v}</span>
    </div>
  )
}

export function CurrentTask(): JSX.Element {
  const { data } = useQuery({ queryKey: ['tasks'], queryFn: api.tasks })
  const t = data?.current
  const done = t ? t.steps.filter((s) => s.status === 'done' || s.status === 'skipped').length : 0
  const pct = t && t.steps.length ? Math.round((done / t.steps.length) * 100) : 0
  const currentStep = t?.steps.find((s) => s.status === 'running') ?? t?.steps.find((s) => s.status === 'pending')

  return (
    <Panel className="p-3.5">
      <div className="mb-2 flex items-center justify-between">
        <span className="panel-label" style={{ letterSpacing: '0.2em' }}>Current task</span>
        <span className="rounded border border-line px-1.5 py-px font-mono text-[8px] uppercase tracking-widest text-faint">live</span>
      </div>
      {!t ? (
        <div className="py-6 text-center text-2xs text-faint">No active task — monitoring the environment.</div>
      ) : (
        <>
          <div className="mb-2 flex items-baseline justify-between gap-2">
            <span className="truncate text-sm text-ink">{t.objective}</span>
            <span className="font-mono text-sm tabular-nums text-accent">{pct}%</span>
          </div>
          <div className="mb-3 h-1 overflow-hidden rounded-full bg-line">
            <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${pct}%`, boxShadow: '0 0 8px var(--accent-glow)' }} />
          </div>
          <Row k="Started" v={ago(t.created_at) + ' ago'} />
          <Row k="Steps completed" v={`${done} / ${t.steps.length}`} />
          {currentStep && (
            <div className="border-t border-line/50 pt-2">
              <div className="panel-label text-[9px]">Current step</div>
              <div className="mt-0.5 text-2xs text-dim">{currentStep.description}</div>
            </div>
          )}
        </>
      )}
    </Panel>
  )
}
