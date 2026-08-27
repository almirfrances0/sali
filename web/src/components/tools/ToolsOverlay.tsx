import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'

import { api } from '../../lib/api'
import { useStore } from '../../stores/store'
import { ago, Dot, riskColor } from '../ui/primitives'

const RISK_LABEL = ['read-only', 'scoped', 'network', 'dangerous', 'critical']

interface ExecRow {
  tool_name: string
  status: string
  result_summary?: string
  started_at?: string
  error?: string | null
}

export function ToolsOverlay(): JSX.Element | null {
  const open = useStore((s) => s.toolsOpen)
  const setOpen = useStore((s) => s.setToolsOpen)
  const { data: cat } = useQuery({ queryKey: ['tools'], queryFn: api.tools, enabled: open })
  const { data: exec } = useQuery({
    queryKey: ['tool-exec'],
    queryFn: () => api.toolExecutions(20),
    enabled: open,
    refetchInterval: open ? 5000 : false,
  })

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [setOpen])

  if (!open) return null
  const tools = cat?.tools ?? []
  const executions = (exec?.executions ?? []) as ExecRow[]

  return (
    <div
      className="absolute inset-0 z-50 flex items-center justify-center bg-base/60 p-6 backdrop-blur-sm"
      onClick={() => setOpen(false)}
    >
      <div
        className="flex max-h-[86%] w-[min(880px,95%)] animate-fade-in flex-col overflow-hidden rounded-lg border border-line-strong bg-panel shadow-panel"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex shrink-0 items-center justify-between border-b border-line px-4 py-2.5">
          <span className="panel-label">Sali's tools — {tools.length} capabilities</span>
          <button onClick={() => setOpen(false)} className="font-mono text-2xs text-faint hover:text-ink">
            ✕ esc
          </button>
        </header>
        <div className="grid min-h-0 flex-1 grid-cols-2 gap-0 divide-x divide-line">
          <div className="min-h-0 overflow-y-auto px-3 py-2.5">
            <div className="panel-label mb-2">catalog</div>
            <div className="space-y-1">
              {tools.map((t) => (
                <div key={t.name} className="flex items-center gap-2 rounded px-1.5 py-1 hover:bg-raised/50">
                  <span title={`risk: ${RISK_LABEL[t.risk] ?? t.risk}`}>
                    <Dot color={riskColor(t.risk)} />
                  </span>
                  <span className="w-40 shrink-0 truncate font-mono text-2xs text-ink">{t.name}</span>
                  <span className="truncate text-2xs text-faint">{t.description}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="min-h-0 overflow-y-auto px-3 py-2.5">
            <div className="panel-label mb-2">recent executions</div>
            {executions.length === 0 ? (
              <div className="text-2xs text-faint">No tool runs recorded yet.</div>
            ) : (
              <div className="space-y-1">
                {executions.map((r, i) => {
                  const ok = r.status === 'verified_success'
                  return (
                    <div key={i} className="flex items-center gap-2 rounded px-1.5 py-1">
                      <span className="w-8 shrink-0 text-right font-mono text-2xs text-faint">
                        {r.started_at ? ago(r.started_at) : ''}
                      </span>
                      <Dot color={ok ? 'var(--ok)' : r.status === 'executing' ? 'var(--warn)' : 'var(--bad)'} />
                      <span className="w-28 shrink-0 truncate font-mono text-2xs text-ink">{r.tool_name}</span>
                      <span className="truncate text-2xs text-dim">{r.result_summary ?? r.error ?? r.status}</span>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
