import { useQuery } from '@tanstack/react-query'

import { api } from '../../lib/api'
import { Panel } from '../ui/primitives'

function Stat({ label, value, unit }: { label: string; value: string; unit?: string }): JSX.Element {
  return (
    <div className="flex-1 border-l border-line/60 px-3 first:border-l-0 first:pl-0">
      <div className="panel-label text-[9px]">{label}</div>
      <div className="mt-1 font-mono text-xl tabular-nums text-ink">{value}</div>
      {unit && <div className="mt-0.5 text-2xs text-faint">{unit}</div>}
    </div>
  )
}

export function SystemState(): JSX.Element {
  const { data: world } = useQuery({ queryKey: ['world'], queryFn: api.world })
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health })
  const mem = world?.memory
  const disk = world?.disk
  return (
    <Panel className="p-3.5">
      <div className="mb-2.5 flex items-center justify-between">
        <span className="panel-label" style={{ letterSpacing: '0.2em' }}>System state</span>
        <span className="rounded border border-line px-1.5 py-px font-mono text-[8px] uppercase tracking-widest text-faint">live</span>
      </div>
      <div className="flex">
        <Stat label="Memory" value={mem ? `${(mem.available_mib / 1024).toFixed(1)} GB` : '—'} unit="available" />
        <Stat label="CPU Load" value={world?.cpu_pct != null ? `${Math.round(world.cpu_pct)}%` : '—'} />
        <Stat label="Disk Usage" value={disk ? `${Math.round(disk.percent_used)}%` : '—'} unit={disk ? `/ ${(disk.total_gib / 1024).toFixed(2)} TB` : undefined} />
        <Stat label="Network" value={health?.internet ? 'Online' : 'Offline'} unit={`${world?.listen_ports ?? '—'} ports`} />
      </div>
    </Panel>
  )
}
