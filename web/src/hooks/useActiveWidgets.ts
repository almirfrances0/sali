import { useEffect, useMemo, useState } from 'react'

import { useStore } from '../stores/store'

// Which process widgets should be on screen RIGHT NOW. Sali "raises" a widget only while it is actually
// doing that thing — driven by real events + presence — and it tucks away when the activity subsides.
// The user can also pin a widget open. Re-evaluated every 2s so widgets fade out on their own.
export const ALL_WIDGETS = ['world', 'tasks', 'tools', 'activity', 'attention', 'schedules'] as const
export type WidgetId = (typeof ALL_WIDGETS)[number]

export function useActiveWidgets(): Set<WidgetId> {
  const events = useStore((s) => s.events)
  const presence = useStore((s) => s.presence?.presence)
  const pinned = useStore((s) => s.pinned)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 2000)
    return () => clearInterval(id)
  }, [])

  return useMemo(() => {
    const now = Date.now()
    const recent = (secs: number, pred: (t: string, p: Record<string, unknown>) => boolean): boolean =>
      events.some((e) => now - Date.parse(e.at) < secs * 1000 && pred(e.type, e.payload))

    const active = new Set<WidgetId>(pinned as WidgetId[])
    // World surfaces on SIGNIFICANT change (structural twin change or a notable observation) — not the
    // steady routine file churn, so it appears when Sali actually has something to show.
    if (recent(50, (t, p) => t.startsWith('twin.') ||
      (t === 'desktop.observed' && ['important', 'critical'].includes(String(p.tier)))))
      active.add('world')
    if (recent(90, (t) => t.startsWith('task.'))) active.add('tasks')
    if (presence === 'executing' || recent(30, (t) => t.startsWith('tool.'))) active.add('tools')
    if (recent(120, (t) => t === 'schedule.fired')) active.add('schedules')
    if (recent(60, (t, p) => t === 'desktop.observed' && ['important', 'critical'].includes(String(p.tier))))
      active.add('attention')
    // Activity only when there's a genuine burst (not the steady background hum)
    const burst = events.filter((e) => now - Date.parse(e.at) < 10000 && e.type !== 'self.presence').length
    if (burst >= 4) active.add('activity')
    return active
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, presence, pinned, tick]) // tick drives the periodic re-evaluation so widgets fade out on time
}
