import { useQuery } from '@tanstack/react-query'

import { api } from '../../lib/api'
import type { Task } from '../../lib/types'
import { Empty, Panel, PanelHeader } from '../ui/primitives'

const STEP_COLOR: Record<string, string> = {
  done: 'var(--ok)',
  running: 'var(--accent)',
  failed: 'var(--bad)',
  skipped: 'var(--faint)',
  pending: 'var(--faint)',
}

function TaskCard({ t }: { t: Task }): JSX.Element {
  const done = t.steps.filter((s) => s.status === 'done' || s.status === 'skipped').length
  return (
    <div className="animate-fade-in rounded-md border border-line bg-base/40 px-3 py-2">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-xs text-ink">{t.objective}</span>
        <span className="shrink-0 font-mono text-2xs text-faint">
          {done}/{t.steps.length}
        </span>
      </div>
      <div className="mt-2 space-y-1">
        {t.steps.map((s) => (
          <div key={s.seq} className="flex items-center gap-2">
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: STEP_COLOR[s.status] ?? 'var(--faint)' }} />
            <span
              className={`truncate text-2xs ${s.status === 'done' ? 'text-faint line-through' : 'text-dim'}`}
            >
              {s.description}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

export function Tasks(): JSX.Element {
  const { data } = useQuery({ queryKey: ['tasks'], queryFn: api.tasks, refetchInterval: 8000 })
  const open = data?.open ?? []
  return (
    <Panel>
      <PanelHeader label="Tasks" id="tasks" right={<span className="font-mono text-2xs text-faint">{open.length} open</span>} />
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2 py-2">
        {open.length === 0 ? <Empty>No active tasks.</Empty> : open.map((t) => <TaskCard key={t.id} t={t} />)}
      </div>
    </Panel>
  )
}
