import { useQuery } from '@tanstack/react-query'

import { api } from '../../lib/api'
import { useStore } from '../../stores/store'
import { ago, Dot, Empty, Panel, PanelHeader } from '../ui/primitives'

interface ExecRow {
  tool_name: string
  status: string
  result_summary?: string
  started_at?: string
  error?: string | null
}

// What Sali is DOING with its hands right now — recent tool executions (live). Header opens the full
// catalog. Appears in the rail only while tools are actually running (§20).
export function ToolsExec(): JSX.Element {
  const setToolsOpen = useStore((s) => s.setToolsOpen)
  const { data } = useQuery({ queryKey: ['tool-exec'], queryFn: () => api.toolExecutions(15), refetchInterval: 4000 })
  const rows = (data?.executions ?? []) as ExecRow[]
  return (
    <Panel>
      <PanelHeader
        label="Tools"
        id="tools"
        right={
          <button onClick={() => setToolsOpen(true)} className="font-mono text-2xs text-accent-dim hover:text-accent">
            catalog ▸
          </button>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto px-2 py-1.5">
        {rows.length === 0 ? (
          <Empty>No tool runs yet.</Empty>
        ) : (
          rows.map((r, i) => {
            const ok = r.status === 'verified_success'
            return (
              <div key={i} className="flex items-center gap-2 px-1 py-0.5">
                <span className="w-7 shrink-0 text-right font-mono text-2xs text-faint">{r.started_at ? ago(r.started_at) : ''}</span>
                <Dot color={ok ? 'var(--ok)' : r.status === 'executing' ? 'var(--warn)' : 'var(--bad)'} pulse={r.status === 'executing'} />
                <span className="w-24 shrink-0 truncate font-mono text-2xs text-ink">{r.tool_name}</span>
                <span className="truncate text-2xs text-dim">{r.result_summary ?? r.error ?? r.status}</span>
              </div>
            )
          })
        )}
      </div>
    </Panel>
  )
}
