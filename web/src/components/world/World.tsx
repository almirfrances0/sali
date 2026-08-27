import { useQuery } from '@tanstack/react-query'

import { api } from '../../lib/api'
import { Empty, Panel, PanelHeader } from '../ui/primitives'

function Row({ k, v }: { k: string; v: string | null | undefined }): JSX.Element | null {
  if (!v) return null
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <span className="panel-label shrink-0">{k}</span>
      <span className="truncate font-mono text-2xs text-dim" title={v}>
        {v}
      </span>
    </div>
  )
}

export function World(): JSX.Element {
  const { data: self } = useQuery({ queryKey: ['self'], queryFn: api.self, refetchInterval: 15000 })
  const { data: world } = useQuery({ queryKey: ['world'], queryFn: api.world, refetchInterval: 6000 })
  const env = self?.environment ?? {}

  return (
    <Panel className="min-h-0 flex-[1.3]">
      <PanelHeader label="World" right={<span className="font-mono text-2xs text-faint">this machine</span>} />
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-2.5">
        <div>
          <Row k="machine" v={env.machine} />
          <Row k="kernel" v={env.kernel} />
          <Row k="model" v={env.model} />
          <Row k="workspace" v={env.workspace} />
        </div>

        <div className="border-t border-line pt-2">
          <Row k="focus" v={world?.focused_app} />
          <Row k="window" v={world?.focused_window} />
          <Row k="task" v={world?.active_task} />
        </div>

        {world && world.recent_commands.length > 0 && (
          <div className="border-t border-line pt-2">
            <div className="panel-label mb-1">recent commands</div>
            {world.recent_commands.slice(0, 5).map((c, i) => (
              <div key={i} className="flex items-center gap-2 py-0.5">
                <span
                  className="h-1.5 w-1.5 rounded-full"
                  style={{ background: c.ok === false ? 'var(--bad)' : c.ok === true ? 'var(--ok)' : 'var(--faint)' }}
                />
                <span className="truncate font-mono text-2xs text-dim">{c.binary}</span>
              </div>
            ))}
          </div>
        )}

        {world && world.recent_files.length > 0 && (
          <div className="border-t border-line pt-2">
            <div className="panel-label mb-1">recent files</div>
            {world.recent_files.slice(0, 5).map((f, i) => (
              <div key={i} className="truncate py-0.5 font-mono text-2xs text-dim" title={f}>
                {f}
              </div>
            ))}
          </div>
        )}

        {world && world.recent_errors.length > 0 && (
          <div className="border-t border-line pt-2">
            <div className="panel-label mb-1 text-bad">recent errors</div>
            {world.recent_errors.slice(0, 3).map((e, i) => (
              <div key={i} className="truncate py-0.5 font-mono text-2xs text-bad/80" title={e}>
                {e}
              </div>
            ))}
          </div>
        )}

        {!self && !world && <Empty>reading the world model…</Empty>}
      </div>
    </Panel>
  )
}
