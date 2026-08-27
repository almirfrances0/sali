import { useMemo, useState } from 'react'

import { useStore } from '../../stores/store'
import { chatSocket } from '../../websocket/useSaliStream'
import { Dot } from '../ui/primitives'

// Sali speaking unprompted (§17). Driven by real `sali.proactive` events on the firehose — never a fake
// nudge. Almir can ask Sali to explain, inspect the trigger, or dismiss.
export function Proactive(): JSX.Element | null {
  const events = useStore((s) => s.events)
  const select = useStore((s) => s.select)
  const pushUser = useStore((s) => s.pushUser)
  const [dismissed, setDismissed] = useState<Set<number>>(new Set())

  const active = useMemo(
    () => events.filter((e) => e.type === 'sali.proactive' && !dismissed.has(e.seq)).slice(-2),
    [events, dismissed],
  )
  if (active.length === 0) return null

  return (
    <div className="pointer-events-none absolute bottom-4 left-1/2 z-40 flex w-[min(560px,90%)] -translate-x-1/2 flex-col gap-2">
      {active.map((e) => {
        const message = String(e.payload.message ?? e.payload.body ?? 'I noticed something.')
        return (
          <div
            key={e.seq}
            className="pointer-events-auto animate-fade-in rounded-lg border border-line-strong bg-panel/95 p-3.5 shadow-panel backdrop-blur-md"
          >
            <div className="mb-1.5 flex items-center gap-2">
              <Dot color="var(--accent)" pulse />
              <span className="panel-label" style={{ color: 'var(--accent)' }}>
                Sali
              </span>
            </div>
            <div className="text-sm text-ink">{message}</div>
            <div className="mt-2.5 flex gap-2">
              <button
                onClick={() => {
                  const q = `Explain what you noticed: ${message}`
                  pushUser(q)
                  chatSocket.message(q)
                  setDismissed((d) => new Set(d).add(e.seq))
                }}
                className="rounded border border-accent-dim bg-accent/10 px-2.5 py-1 font-mono text-2xs uppercase tracking-wider text-accent hover:bg-accent/20"
              >
                Explain
              </button>
              <button
                onClick={() => select({ kind: 'event', id: String(e.seq), label: e.type })}
                className="rounded border border-line px-2.5 py-1 font-mono text-2xs uppercase tracking-wider text-dim hover:border-line-strong"
              >
                Inspect
              </button>
              <button
                onClick={() => setDismissed((d) => new Set(d).add(e.seq))}
                className="ml-auto rounded px-2.5 py-1 font-mono text-2xs uppercase tracking-wider text-faint hover:text-ink"
              >
                Dismiss
              </button>
            </div>
          </div>
        )
      })}
    </div>
  )
}
