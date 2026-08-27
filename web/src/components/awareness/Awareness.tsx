import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'

import { api } from '../../lib/api'
import { useStore } from '../../stores/store'
import { ago, Dot, Panel } from '../ui/primitives'

function Field({ label, value }: { label: string; value: string | null | undefined }): JSX.Element | null {
  if (!value) return null
  return (
    <div className="py-1">
      <div className="panel-label text-[9px]">{label}</div>
      <div className="truncate font-mono text-2xs text-ink" title={value}>{value}</div>
    </div>
  )
}

function KV({ k, v, color }: { k: string; v: React.ReactNode; color?: string }): JSX.Element {
  return (
    <div className="flex items-center justify-between py-0.5">
      <span className="text-2xs text-dim">{k}</span>
      <span className="font-mono text-2xs tabular-nums" style={{ color: color ?? 'var(--ink)' }}>{v}</span>
    </div>
  )
}

function Head({ title, live }: { title: string; live?: boolean }): JSX.Element {
  return (
    <div className="mb-1.5 flex items-center justify-between">
      <span className="panel-label" style={{ color: 'var(--accent-dim)', letterSpacing: '0.2em' }}>{title}</span>
      {live && <span className="rounded border border-line px-1.5 py-px font-mono text-[8px] uppercase tracking-widest text-faint">live</span>}
    </div>
  )
}

const TIERS = [
  { k: 'critical', label: 'Critical', c: 'var(--critical)' },
  { k: 'important', label: 'Important', c: 'var(--important)' },
  { k: 'interesting', label: 'Interesting', c: 'var(--interesting)' },
  { k: 'routine', label: 'Routine', c: 'var(--routine)' },
] as const

export function Awareness(): JSX.Element {
  const { data: self } = useQuery({ queryKey: ['self'], queryFn: api.self })
  const { data: world } = useQuery({ queryKey: ['world'], queryFn: api.world })
  const { data: attn } = useQuery({ queryKey: ['attention'], queryFn: () => api.attention(24) })
  const events = useStore((s) => s.events)
  const env = self?.environment ?? {}
  const project = env.workspace ? env.workspace.split('/').filter(Boolean).pop() : undefined

  const noticed = useMemo(
    () => events.filter((e) => e.type === 'desktop.observed').slice(-5).reverse(),
    [events],
  )
  const counts: Record<string, number | null> = {
    critical: attn?.critical ?? 0, important: attn?.important ?? 0,
    interesting: attn?.interesting ?? 0, routine: attn?.routine ?? null,
  }

  return (
    <Panel className="overflow-y-auto">
      <div className="space-y-4 px-3.5 py-3">
        <div>
          <Head title="Awareness" live />
          <div className="mb-1 font-mono text-2xs uppercase tracking-widest text-accent">now</div>
          <Field label="Focused window" value={world?.focused_window ?? world?.focused_app} />
          <Field label="Active project" value={project} />
          <Field label="Working directory" value={env.workspace} />
          <Field label="User" value="Almir" />
          <Field label="Machine" value={env.machine} />
        </div>

        <div className="border-t border-line/60 pt-3">
          <Head title="Attention" />
          {TIERS.map((t) => (
            <div key={t.k} className="flex items-center gap-2 py-0.5">
              <Dot color={t.c} />
              <span className="text-2xs text-dim">{t.label}</span>
              <span className="ml-auto font-mono text-2xs tabular-nums" style={{ color: t.c }}>{counts[t.k] === null ? '—' : counts[t.k]}</span>
            </div>
          ))}
        </div>

        {noticed.length > 0 && (
          <div className="border-t border-line/60 pt-3">
            <Head title="Recently noticed" live />
            {noticed.map((e) => (
              <div key={e.seq} className="flex items-center gap-2 py-0.5">
                <span className="w-8 shrink-0 font-mono text-2xs text-faint">{ago(e.at)}</span>
                <span className="truncate text-2xs text-dim">{String(e.payload.summary ?? e.payload.kind ?? '')}</span>
              </div>
            ))}
          </div>
        )}

        <div className="border-t border-line/60 pt-3">
          <Head title="World summary" live />
          <KV k="Processes" v={world?.processes ?? '—'} />
          <KV k="Services" v={world?.services ?? '—'} />
          <KV k="Open Ports" v={world?.listen_ports ?? '—'} />
          <KV k="Containers" v={world?.containers ?? '—'} />
          <KV k="Network" v={self ? 'Online' : '—'} color="var(--ok)" />
          <KV k="GPU Temp" v={world?.gpu?.temperature_c != null ? `${world.gpu.temperature_c}°C` : '—'} color="var(--interesting)" />
        </div>
      </div>
    </Panel>
  )
}
