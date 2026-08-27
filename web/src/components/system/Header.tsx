import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'

import { api } from '../../lib/api'
import type { PresenceState, SelfView } from '../../lib/types'
import { presenceColor, useStore } from '../../stores/store'

const STATE_VERB: Record<string, string> = {
  online: 'ONLINE', idle: 'IDLE', observing: 'PERCEIVING', thinking: 'THINKING',
  executing: 'USING A TOOL', waiting: 'WAITING FOR ALMIR', learning: 'LEARNING', offline: 'OFFLINE',
}

function op(presence: PresenceState | undefined, self: SelfView | undefined): string {
  if (!presence) return 'connecting…'
  if (presence.presence === 'idle' || presence.presence === 'observing')
    return self?.current_focus ? self.current_focus : 'monitoring the environment'
  return presence.run_state?.replace(/_/g, ' ') ?? self?.active_operation ?? 'working'
}

function fmtUptime(s: number | null | undefined): string {
  if (!s) return '—'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h ${m}m ${s % 60}s`
}

// a tiny live EEG-style waveform beside the state — driven by the real event rate, not a decorative loop
function Wave({ active }: { active: boolean }): JSX.Element {
  const ref = useRef<HTMLCanvasElement>(null)
  const lastSeq = useStore((s) => s.lastSeq)
  const energy = useRef(0.15)
  useEffect(() => { energy.current = Math.min(1, energy.current + 0.5) }, [lastSeq])
  useEffect(() => {
    let raf = 0
    const draw = () => {
      const c = ref.current
      const ctx = c?.getContext('2d')
      if (c && ctx) {
        energy.current = Math.max(0.12, energy.current - 0.01)
        const w = c.width
        const h = c.height
        ctx.clearRect(0, 0, w, h)
        ctx.beginPath()
        const t = performance.now() / 200
        const amp = active ? energy.current : 0.12
        for (let x = 0; x < w; x++) {
          const y = h / 2 + Math.sin(x * 0.25 + t) * h * 0.32 * amp * (0.4 + 0.6 * Math.sin(x * 0.07 + t * 0.7))
          x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)
        }
        ctx.strokeStyle = 'rgba(120, 220, 255, 0.75)'
        ctx.lineWidth = 1
        ctx.stroke()
      }
      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [active])
  return <canvas ref={ref} width={130} height={22} className="opacity-80" />
}

function Metric({ label, children }: { label: string; children: React.ReactNode }): JSX.Element {
  return (
    <div className="flex flex-col gap-0.5 border-l border-line/70 pl-4 leading-none">
      <span className="panel-label text-[9px]">{label}</span>
      <span className="font-mono text-xs tabular-nums text-ink">{children}</span>
    </div>
  )
}

export function Header(): JSX.Element {
  const { data: presence } = useQuery({ queryKey: ['presence'], queryFn: api.presence })
  const { data: self } = useQuery({ queryKey: ['self'], queryFn: api.self })
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health })
  const { data: world } = useQuery({ queryKey: ['world'], queryFn: api.world })
  const setToolsOpen = useStore((s) => s.setToolsOpen)
  const setSchedulesOpen = useStore((s) => s.setSchedulesOpen)

  const p = presence?.presence
  const color = presenceColor(p)
  const active = p === 'thinking' || p === 'executing' || p === 'learning' || p === 'observing'
  const g = world?.gpu
  const mem = world?.memory

  return (
    <header className="panel flex shrink-0 items-center gap-6 px-5 py-2">
      <div className="flex flex-col leading-none">
        <span className="text-2xl font-bold tracking-[0.18em] text-ink">SALI</span>
        <span className="panel-label mt-1 text-[9px]">Self-aware local intelligence</span>
      </div>

      <div className="flex items-center gap-3">
        <span className="text-sm font-semibold tracking-wide" style={{ color }}>SALI IS {STATE_VERB[p ?? 'online'] ?? 'ONLINE'}…</span>
        <Wave active={active} />
      </div>
      <div className="flex min-w-0 flex-col leading-tight">
        <span className="panel-label text-[9px]">Current operation</span>
        <span className="truncate font-mono text-xs text-dim">{op(presence, self)}</span>
      </div>

      <div className="ml-auto flex items-center gap-4">
        <Metric label="GPU">{g?.name?.replace('NVIDIA GeForce ', '') ?? 'RTX 4070'}</Metric>
        <Metric label="VRAM">{g ? `${(g.vram_used_mib / 1024).toFixed(1)} / ${(g.vram_total_mib / 1024).toFixed(0)}GB` : '—'}</Metric>
        <Metric label="CPU">{world?.cpu_pct != null ? `${Math.round(world.cpu_pct)}%` : '—'}</Metric>
        <Metric label="RAM">{mem ? `${(mem.used_mib / 1024).toFixed(1)} / ${(mem.total_mib / 1024).toFixed(1)}GB` : '—'}</Metric>
        <div className="flex flex-col gap-0.5 border-l border-line/70 pl-4 leading-none">
          <span className="panel-label text-[9px]">Internet</span>
          <span className="font-mono text-xs" style={{ color: health?.internet ? 'var(--ok)' : 'var(--bad)' }}>{health?.internet ? 'ONLINE' : 'OFFLINE'}</span>
        </div>
        <Metric label="Uptime">{fmtUptime(world?.uptime_s)}</Metric>
        <div className="ml-2 flex items-center gap-2 border-l border-line/70 pl-4">
          <button onClick={() => setSchedulesOpen(true)} title="schedules" className="text-faint hover:text-accent">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3"><circle cx="8" cy="9" r="5.5" /><path d="M8 6.5V9l1.8 1" /><path d="M8 1.5v1.5M5 2l1 1.2M11 2l-1 1.2" /></svg>
          </button>
          <button onClick={() => setToolsOpen(true)} title="tools" className="text-faint hover:text-accent">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3"><path d="M6.5 2.5a3 3 0 0 0 4 4l3 3a1.5 1.5 0 0 1-2 2l-3-3a3 3 0 0 1-4-4z" /></svg>
          </button>
        </div>
      </div>
    </header>
  )
}
