import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'

import { Activity } from './components/activity/Activity'
import { Attention } from './components/attention/Attention'
import { Brain } from './components/brain/Brain'
import { Chat } from './components/chat/Chat'
import { ConfirmModal } from './components/chat/ConfirmModal'
import { Inspector } from './components/memory/Inspector'
import { Proactive } from './components/system/Proactive'
import { TopBar } from './components/system/TopBar'
import { Tasks } from './components/tasks/Tasks'
import { World } from './components/world/World'
import { useStore } from './stores/store'
import { useEventStream } from './websocket/useEventStream'
import { useSaliStream } from './websocket/useSaliStream'

// When real events land on the firehose, refresh the cheap snapshot queries they affect — so the cockpit
// is event-driven, not blindly polling. The brain animates from the chat retrieval frame; the graph
// snapshot itself is refreshed lazily (relayout is expensive) only on structural twin changes.
function useLiveInvalidation(): void {
  const qc = useQueryClient()
  const lastSeq = useStore((s) => s.lastSeq)
  const events = useStore((s) => s.events)
  const processed = useRef(0)
  const lastWorld = useRef(0)
  const lastGraph = useRef(0)

  useEffect(() => {
    const fresh = events.filter((e) => e.seq > processed.current)
    processed.current = lastSeq
    if (fresh.length === 0) return
    const types = new Set(fresh.map((e) => e.type))
    const has = (p: string) => [...types].some((t) => t === p || t.startsWith(p))

    if (has('self.presence') || has('conversation.') || has('tool.')) {
      qc.invalidateQueries({ queryKey: ['presence'] })
      qc.invalidateQueries({ queryKey: ['self'] })
    }
    if (has('task.')) qc.invalidateQueries({ queryKey: ['tasks'] })
    if (has('desktop.observed')) {
      const now = Date.now()
      if (now - lastWorld.current > 3000) {
        lastWorld.current = now
        qc.invalidateQueries({ queryKey: ['world'] })
        qc.invalidateQueries({ queryKey: ['attention'] })
      }
    }
    if (has('twin.entity')) {
      const now = Date.now()
      if (now - lastGraph.current > 20000) {
        lastGraph.current = now
        qc.invalidateQueries({ queryKey: ['graph-snapshot'] })
      }
    }
  }, [lastSeq, events, qc])
}

export default function App(): JSX.Element {
  useEventStream()
  useSaliStream()
  useLiveInvalidation()

  return (
    <div className="cockpit relative">
      <TopBar />
      <div className="area-world flex min-h-0 flex-col gap-2.5">
        <World />
        <Attention />
      </div>
      <Brain />
      <div className="area-activity flex min-h-0 flex-col gap-2.5">
        <Activity />
        <Tasks />
      </div>
      <Chat />

      <Inspector />
      <Proactive />
      <ConfirmModal />
    </div>
  )
}
