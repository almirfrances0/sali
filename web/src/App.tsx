import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'

import { Brain } from './components/brain/Brain'
import { Chat } from './components/chat/Chat'
import { ConfirmModal } from './components/chat/ConfirmModal'
import { Inspector } from './components/memory/Inspector'
import { MiniBar } from './components/system/MiniBar'
import { ProcessRail } from './components/system/ProcessRail'
import { Proactive } from './components/system/Proactive'
import { ToolsOverlay } from './components/tools/ToolsOverlay'
import { useStore } from './stores/store'
import { useEventStream } from './websocket/useEventStream'
import { useSaliStream } from './websocket/useSaliStream'

// Real events refresh the cheap snapshot queries they affect — the cockpit is event-driven, not polling.
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
    if (has('tool.')) qc.invalidateQueries({ queryKey: ['tool-exec'] })
    if (has('task.')) qc.invalidateQueries({ queryKey: ['tasks'] })
    if (has('schedule.')) qc.invalidateQueries({ queryKey: ['schedules'] })
    if (has('desktop.observed')) {
      const now = Date.now()
      if (now - lastWorld.current > 3000) {
        lastWorld.current = now
        qc.invalidateQueries({ queryKey: ['world'] })
        qc.invalidateQueries({ queryKey: ['attention'] })
      }
    }
    if (has('twin.entity') || has('memory.created')) {
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
    <div className="relative flex h-full flex-col gap-2.5 p-2.5">
      <MiniBar />
      <div className="flex min-h-0 flex-1 gap-2.5">
        {/* Sali raises process widgets here only while it is doing that thing; empty when calm. */}
        <ProcessRail />
        {/* The neural memory and the conversation are always present. */}
        <div className="min-w-0 flex-1">
          <Brain />
        </div>
        <div className="w-[clamp(340px,28%,520px)] shrink-0">
          <Chat />
        </div>
      </div>

      <Inspector />
      <Proactive />
      <ToolsOverlay />
      <ConfirmModal />
    </div>
  )
}
