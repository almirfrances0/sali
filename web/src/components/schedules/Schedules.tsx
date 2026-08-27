import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { api } from '../../lib/api'
import type { Schedule } from '../../lib/types'
import { ago, Dot, Empty, Panel, PanelHeader } from '../ui/primitives'

export function Schedules(): JSX.Element {
  const qc = useQueryClient()
  const { data } = useQuery({ queryKey: ['schedules'], queryFn: api.schedules})
  const list = data?.schedules ?? []
  const [name, setName] = useState('')
  const [when, setWhen] = useState('')
  const [prompt, setPrompt] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = () => qc.invalidateQueries({ queryKey: ['schedules'] })

  const create = async () => {
    setErr(null)
    setBusy(true)
    try {
      await api.createSchedule(name.trim(), when.trim(), prompt.trim())
      setName('')
      setWhen('')
      setPrompt('')
      refresh()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed')
    } finally {
      setBusy(false)
    }
  }

  const remove = async (n: string) => {
    await api.deleteSchedule(n)
    refresh()
  }

  return (
    <Panel>
      <PanelHeader label="Schedules" id="schedules" right={<span className="font-mono text-2xs text-faint">{list.length}</span>} />
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-3 py-2.5">
        {list.length === 0 ? (
          <Empty>No schedules yet — create one below.</Empty>
        ) : (
          list.map((s: Schedule) => (
            <div key={s.id} className="animate-fade-in rounded-md border border-line bg-base/40 px-2.5 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-2 truncate">
                  <Dot color={s.enabled ? 'var(--ok)' : 'var(--faint)'} />
                  <span className="truncate text-xs text-ink">{s.name}</span>
                </span>
                <button onClick={() => remove(s.name)} className="shrink-0 font-mono text-2xs text-faint hover:text-bad" title="delete">
                  ✕
                </button>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5 font-mono text-2xs text-dim">
                <span className="chip">{s.kind}</span>
                <span className="chip">{s.spec}</span>
                {s.next_run_at && <span className="text-faint">next in {ago(s.next_run_at).replace('-', '')}…</span>}
              </div>
              <div className="mt-1 truncate text-2xs text-faint" title={s.prompt}>
                “{s.prompt}”
              </div>
            </div>
          ))
        )}
      </div>
      <div className="shrink-0 space-y-1.5 border-t border-line px-3 py-2.5">
        <div className="flex gap-1.5">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="name"
            className="w-1/2 rounded border border-line bg-base/60 px-2 py-1 text-2xs text-ink outline-none placeholder:text-faint focus:border-line-strong" />
          <input value={when} onChange={(e) => setWhen(e.target.value)} placeholder="30m or 0 9 * * *"
            className="w-1/2 rounded border border-line bg-base/60 px-2 py-1 font-mono text-2xs text-ink outline-none placeholder:text-faint focus:border-line-strong" />
        </div>
        <div className="flex gap-1.5">
          <input value={prompt} onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && name && when && prompt && create()}
            placeholder="what Sali should do…"
            className="flex-1 rounded border border-line bg-base/60 px-2 py-1 text-2xs text-ink outline-none placeholder:text-faint focus:border-line-strong" />
          <button onClick={create} disabled={busy || !name || !when || !prompt}
            className="rounded border border-accent-dim bg-accent/10 px-2.5 py-1 font-mono text-2xs uppercase tracking-wider text-accent hover:bg-accent/20 disabled:opacity-40">
            add
          </button>
        </div>
        {err && <div className="text-2xs text-bad">{err}</div>}
      </div>
    </Panel>
  )
}
