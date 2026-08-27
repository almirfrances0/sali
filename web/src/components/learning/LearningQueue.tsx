import { useQuery } from '@tanstack/react-query'

import { api } from '../../lib/api'
import { Empty, Panel } from '../ui/primitives'

interface Item { kind: string; subject: string; priority: number }

function badge(priority: number): { label: string; color: string } {
  if (priority <= 3) return { label: 'HIGH', color: 'var(--critical)' }
  if (priority <= 6) return { label: 'MEDIUM', color: 'var(--important)' }
  return { label: 'LOW', color: 'var(--faint)' }
}

export function LearningQueue(): JSX.Element {
  const { data } = useQuery({ queryKey: ['learning'], queryFn: api.learningQueue })
  const items = (data?.pending ?? []) as Item[]
  return (
    <Panel className="p-3.5">
      <div className="mb-2 flex items-center justify-between">
        <span className="panel-label" style={{ letterSpacing: '0.2em' }}>Learning queue</span>
        <span className="rounded border border-line px-1.5 py-px font-mono text-[8px] uppercase tracking-widest text-faint">live</span>
      </div>
      {items.length === 0 ? (
        <Empty>Nothing queued to learn.</Empty>
      ) : (
        <div className="space-y-2">
          {items.slice(0, 5).map((it, i) => {
            const b = badge(it.priority)
            return (
              <div key={i} className="flex items-center justify-between gap-2">
                <span className="truncate text-2xs text-dim">{it.subject}</span>
                <span className="shrink-0 font-mono text-[9px] font-semibold tracking-wider" style={{ color: b.color }}>{b.label}</span>
              </div>
            )
          })}
          {items.length > 5 && <div className="pt-1 text-2xs text-faint">{items.length} items in queue</div>}
        </div>
      )}
    </Panel>
  )
}
