import { useQuery } from '@tanstack/react-query'

import { api } from '../../lib/api'
import type { PresenceState, SelfView } from '../../lib/types'
import { presenceColor, useStore } from '../../stores/store'
import { ALL_WIDGETS, useActiveWidgets, type WidgetId } from '../../hooks/useActiveWidgets'
import { Dot } from '../ui/primitives'

const PRESENCE_LABEL: Record<string, string> = {
  online: 'Online', idle: 'Idle', observing: 'Observing', thinking: 'Thinking',
  executing: 'Executing', waiting: 'Waiting for Almir', learning: 'Learning', offline: 'Offline',
}
const WIDGET_LABEL: Record<WidgetId, string> = {
  world: 'World', tasks: 'Tasks', tools: 'Tools', activity: 'Activity', attention: 'Attention', schedules: 'Schedules',
}

function op(presence: PresenceState | undefined, self: SelfView | undefined): string {
  if (!presence) return 'connecting…'
  if (presence.presence === 'idle' || presence.presence === 'observing')
    return self?.current_focus ? `last: ${self.current_focus}` : 'monitoring the environment'
  return presence.run_state?.replace(/_/g, ' ') ?? self?.active_operation ?? 'working'
}

export function MiniBar(): JSX.Element {
  const { data: presence } = useQuery({ queryKey: ['presence'], queryFn: api.presence, refetchInterval: 4000 })
  const { data: self } = useQuery({ queryKey: ['self'], queryFn: api.self, refetchInterval: 15000 })
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 12000 })
  const streamState = useStore((s) => s.streamState)
  const chatState = useStore((s) => s.chatState)
  const pinned = useStore((s) => s.pinned)
  const togglePinned = useStore((s) => s.togglePinned)
  const active = useActiveWidgets()

  const p = presence?.presence
  const color = presenceColor(p)

  return (
    <header className="panel flex shrink-0 items-center gap-4 px-4 py-2">
      <div className="flex items-baseline gap-3">
        <span className="font-mono text-base font-semibold tracking-[0.32em] text-ink">SALI</span>
        <span className="flex items-center gap-1.5">
          <Dot color={color} pulse={p === 'thinking' || p === 'executing'} />
          <span className="text-xs font-medium" style={{ color }}>{PRESENCE_LABEL[p ?? 'online'] ?? 'Online'}</span>
        </span>
      </div>
      <span className="min-w-0 truncate font-mono text-2xs text-faint">{op(presence, self)}</span>

      {/* widget pills — Sali auto-raises active ones; click to pin one open (or close it) */}
      <div className="ml-auto flex items-center gap-1">
        {ALL_WIDGETS.map((w) => {
          const on = active.has(w)
          const isPinned = pinned.includes(w)
          return (
            <button
              key={w}
              onClick={() => togglePinned(w)}
              className={`rounded-full border px-2 py-0.5 font-mono text-2xs transition-colors ${
                on ? 'border-accent-dim text-accent' : 'border-line text-faint hover:text-dim'
              }`}
              title={isPinned ? 'pinned open — click to unpin' : on ? 'active' : 'click to open'}
            >
              {isPinned ? '📌 ' : ''}{WIDGET_LABEL[w]}
            </button>
          )
        })}
      </div>

      <div className="flex items-center gap-1.5 border-l border-line pl-3">
        {health && Object.entries(health.subsystems).map(([name, ok]) => (
          <span key={name} title={`${name}: ${ok ? 'ok' : 'down'}`}><Dot color={ok ? 'var(--ok)' : 'var(--bad)'} /></span>
        ))}
        <span title={`events: ${streamState}`}><Dot color={streamState === 'open' ? 'var(--ok)' : 'var(--warn)'} /></span>
        <span title={`chat: ${chatState}`}><Dot color={chatState === 'open' ? 'var(--ok)' : 'var(--warn)'} /></span>
      </div>
    </header>
  )
}
