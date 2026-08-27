import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'

import { api } from '../../lib/api'
import { useStore } from '../../stores/store'
import { ago, Panel, PanelHeader } from '../ui/primitives'

const TIERS = [
  { k: 'critical', label: 'Critical', c: 'var(--critical)' },
  { k: 'important', label: 'Important', c: 'var(--important)' },
  { k: 'interesting', label: 'Interesting', c: 'var(--interesting)' },
  { k: 'routine', label: 'Routine', c: 'var(--routine)' },
] as const

export function Attention(): JSX.Element {
  const { data } = useQuery({ queryKey: ['attention'], queryFn: () => api.attention(24), refetchInterval: 15000 })
  const events = useStore((s) => s.events)

  // live notable observations (important/critical) straight from the firehose
  const notable = useMemo(
    () =>
      events
        .filter((e) => e.type === 'desktop.observed' && ['important', 'critical'].includes(String(e.payload.tier)))
        .slice(-6)
        .reverse(),
    [events],
  )

  const counts: Record<string, number | null> = {
    critical: data?.critical ?? 0,
    important: data?.important ?? 0,
    interesting: data?.interesting ?? 0,
    routine: data?.routine ?? null,
  }

  return (
    <Panel>
      <PanelHeader label="Attention" id="attention" right={<span className="font-mono text-2xs text-faint">24h</span>} />
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2.5">
        <div className="grid grid-cols-2 gap-2">
          {TIERS.map((t) => (
            <div key={t.k} className="flex items-center gap-2 rounded-md border border-line bg-base/40 px-2 py-1.5">
              <span className="h-2 w-2 rounded-full" style={{ background: t.c, boxShadow: `0 0 8px ${t.c}` }} />
              <span className="text-2xs text-dim">{t.label}</span>
              <span className="ml-auto font-mono text-xs tabular-nums" style={{ color: t.c }}>
                {counts[t.k] === null ? '—' : counts[t.k]}
              </span>
            </div>
          ))}
        </div>

        {notable.length > 0 && (
          <div className="mt-3 border-t border-line pt-2">
            <div className="panel-label mb-1">notable now</div>
            {notable.map((e) => (
              <div key={e.seq} className="flex items-center gap-2 py-0.5">
                <span className="w-8 shrink-0 text-right font-mono text-2xs text-faint">{ago(e.at)}</span>
                <span
                  className="h-1.5 w-1.5 shrink-0 rounded-full"
                  style={{ background: e.payload.tier === 'critical' ? 'var(--critical)' : 'var(--important)' }}
                />
                <span className="truncate text-2xs text-dim">{String(e.payload.summary ?? e.payload.kind ?? '')}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </Panel>
  )
}
