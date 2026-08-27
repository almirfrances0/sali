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

// Terminal and web are the SAME Sali on the SAME event bus. The dashboard NEVER polls: each snapshot is
// read once, then refreshed ONLY when a real `event` fires on the firehose — exactly how the terminal /
// daemon react to `sali_events`. A gap in the stream (reconnect) triggers a full resync.
function useLiveSync(): void {
  const qc = useQueryClient()
  const lastSeq = useStore((s) => s.lastSeq)
  const events = useStore((s) => s.events)
  const streamState = useStore((s) => s.streamState)
  const processed = useRef(0)
  const at = useRef<Record<string, number>>({})
  const wasOpen = useRef(false)

  // resync everything when the firehose (re)connects — events during the gap were missed
  useEffect(() => {
    if (streamState === 'open' && !wasOpen.current) qc.invalidateQueries()
    wasOpen.current = streamState === 'open'
  }, [streamState, qc])

  useEffect(() => {
    const fresh = events.filter((e) => e.seq > processed.current)
    processed.current = lastSeq
    if (fresh.length === 0) return
    const types = new Set(fresh.map((e) => e.type))
    const has = (...ps: string[]) => ps.some((p) => [...types].some((x) => x === p || x.startsWith(p)))
    const now = Date.now()
    const bump = (key: string, minMs = 0) => {
      if (now - (at.current[key] ?? 0) >= minMs) {
        at.current[key] = now
        qc.invalidateQueries({ queryKey: [key] })
      }
    }
    if (has('self.presence', 'conversation.', 'tool.', 'sali.', 'schedule.', 'desktop.observed')) bump('presence', 1200)
    if (has('self.presence', 'conversation.', 'memory.', 'twin.', 'learning.')) bump('self', 3000)
    if (has('tool.')) bump('tool-exec')
    if (has('task.')) bump('tasks')
    if (has('schedule.')) bump('schedules')
    if (has('desktop.observed', 'twin.')) {
      bump('world', 3000)
      bump('attention', 5000)
    }
    if (has('twin.entity', 'memory.created')) bump('graph-snapshot', 20000)
  }, [lastSeq, events, qc])
}

export default function App(): JSX.Element {
  useEventStream()
  useSaliStream()
  useLiveSync()

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
