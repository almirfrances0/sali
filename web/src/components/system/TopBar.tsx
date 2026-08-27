import { useQuery } from '@tanstack/react-query'

import { api } from '../../lib/api'
import type { AttentionCounts, Health, PresenceState, SelfView } from '../../lib/types'
import { presenceColor, useStore } from '../../stores/store'
import { Dot } from '../ui/primitives'

const PRESENCE_LABEL: Record<string, string> = {
  online: 'Online',
  idle: 'Idle',
  observing: 'Observing',
  thinking: 'Thinking',
  executing: 'Executing',
  waiting: 'Waiting for Almir',
  learning: 'Learning',
  offline: 'Offline',
}

function operationLine(presence: PresenceState | undefined, self: SelfView | undefined): string {
  if (!presence) return 'connecting…'
  if (presence.presence === 'idle' || presence.presence === 'observing')
    return self?.current_focus ? `last: ${self.current_focus}` : 'monitoring the environment'
  if (presence.run_state) return presence.run_state.replace(/_/g, ' ')
  return self?.active_operation ?? self?.current_focus ?? 'working'
}

function Conn({ label, state }: { label: string; state: string }): JSX.Element {
  const color = state === 'open' ? 'var(--ok)' : state === 'connecting' ? 'var(--warn)' : 'var(--bad)'
  return (
    <span className="chip" title={`${label}: ${state}`}>
      <Dot color={color} /> {label}
    </span>
  )
}

export function TopBar(): JSX.Element {
  const { data: presence } = useQuery({ queryKey: ['presence'], queryFn: api.presence, refetchInterval: 4000 })
  const { data: self } = useQuery({ queryKey: ['self'], queryFn: api.self, refetchInterval: 15000 })
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 12000 })
  const { data: attn } = useQuery({ queryKey: ['attention'], queryFn: () => api.attention(24), refetchInterval: 20000 })
  const streamState = useStore((s) => s.streamState)
  const chatState = useStore((s) => s.chatState)

  const p = presence?.presence
  const color = presenceColor(p)
  const env = self?.environment

  return (
    <header className="panel flex shrink-0 items-center gap-5 px-4 py-2.5">
      <div className="flex items-baseline gap-3">
        <span className="font-mono text-lg font-semibold tracking-[0.35em] text-ink">SALI</span>
        <span className="flex items-center gap-2 rounded-full border border-line px-2.5 py-1">
          <Dot color={color} pulse={p === 'thinking' || p === 'executing'} />
          <span className="text-xs font-medium" style={{ color }}>
            {PRESENCE_LABEL[p ?? 'online'] ?? 'Online'}
          </span>
        </span>
      </div>

      <div className="flex min-w-0 flex-col leading-tight">
        <span className="panel-label">current operation</span>
        <span className="truncate font-mono text-xs text-dim">{operationLine(presence, self)}</span>
      </div>

      <div className="ml-auto flex items-center gap-4">
        <AttentionStrip attn={attn} />
        <HealthStrip health={health} env={env} />
        <div className="flex items-center gap-1.5">
          <Conn label="events" state={streamState} />
          <Conn label="chat" state={chatState} />
        </div>
      </div>
    </header>
  )
}

function AttentionStrip({ attn }: { attn: AttentionCounts | undefined }): JSX.Element {
  const tiers = [
    { k: 'critical', c: 'var(--critical)', n: attn?.critical },
    { k: 'important', c: 'var(--important)', n: attn?.important },
    { k: 'interesting', c: 'var(--interesting)', n: attn?.interesting },
  ]
  return (
    <div className="flex items-center gap-2" title="attention over the last 24h">
      {tiers.map((t) => (
        <span key={t.k} className="flex items-center gap-1.5">
          <Dot color={t.c} />
          <span className="font-mono text-2xs text-dim">{t.n ?? '·'}</span>
        </span>
      ))}
    </div>
  )
}

function HealthStrip({ health, env }: { health: Health | undefined; env: Record<string, string> | undefined }): JSX.Element {
  return (
    <div className="flex items-center gap-2">
      {env?.machine && <span className="hidden font-mono text-2xs text-faint lg:inline">{env.machine}</span>}
      {health &&
        Object.entries(health.subsystems).map(([name, ok]) => (
          <span key={name} title={`${name}: ${ok ? 'ok' : 'down'}`}>
            <Dot color={ok ? 'var(--ok)' : 'var(--bad)'} />
          </span>
        ))}
    </div>
  )
}
