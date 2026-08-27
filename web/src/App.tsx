import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'

import { Activity } from './components/activity/Activity'
import { Awareness } from './components/awareness/Awareness'
import { HeadBrain } from './components/brain/HeadBrain'
import { Chat } from './components/chat/Chat'
import { ConfirmModal } from './components/chat/ConfirmModal'
import { LearningQueue } from './components/learning/LearningQueue'
import { Inspector } from './components/memory/Inspector'
import { SchedulesOverlay } from './components/schedules/SchedulesOverlay'
import { Header } from './components/system/Header'
import { Proactive } from './components/system/Proactive'
import { SystemState } from './components/system/SystemState'
import { CurrentTask } from './components/tasks/CurrentTask'
import { ToolsOverlay } from './components/tools/ToolsOverlay'
import { Panel } from './components/ui/primitives'
import { useStore } from './stores/store'
import { useEventStream } from './websocket/useEventStream'
import { useSaliStream } from './websocket/useSaliStream'

// Terminal and web are the SAME Sali on the SAME event bus. The dashboard NEVER polls: each snapshot is
// read once, then refreshed only when a real event fires on the /stream firehose. A gap triggers a resync.
function useLiveSync(): void {
  const qc = useQueryClient()
  const lastSeq = useStore((s) => s.lastSeq)
  const events = useStore((s) => s.events)
  const streamState = useStore((s) => s.streamState)
  const processed = useRef(0)
  const at = useRef<Record<string, number>>({})
  const wasOpen = useRef(false)

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
    if (has('self.presence', 'conversation.', 'tool.', 'sali.', 'schedule.', 'desktop.observed')) {
      bump('presence', 1200)
      bump('world', 4000) // presence/resources shift as Sali works
    }
    if (has('self.presence', 'conversation.', 'memory.', 'twin.', 'learning.')) bump('self', 3000)
    if (has('task.')) bump('tasks')
    if (has('schedule.')) bump('schedules')
    if (has('learning.')) bump('learning', 2000)
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
    <div className="relative flex h-full flex-col gap-2 p-2">
      <Header />
      <div className="flex min-h-0 flex-1 gap-2">
        {/* LEFT — AWARENESS: what Sali perceives, knows about its world, and is attending to */}
        <div className="flex w-[clamp(240px,16%,320px)] shrink-0 flex-col">
          <Awareness />
        </div>

        {/* CENTER — the neural brain dominates; conversation + system state dock beneath it */}
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <Panel className="min-h-0 flex-[2.4] overflow-hidden">
            <HeadBrain />
          </Panel>
          <div className="flex min-h-0 flex-1 gap-2">
            <div className="min-w-0 flex-[1.5]">
              <Chat />
            </div>
            <div className="w-[clamp(260px,34%,440px)] shrink-0">
              <SystemState />
            </div>
          </div>
        </div>

        {/* RIGHT — LIVE ACTIVITY, CURRENT TASK, LEARNING QUEUE */}
        <div className="flex w-[clamp(250px,19%,340px)] shrink-0 flex-col gap-2">
          <div className="min-h-0 flex-[1.4]">
            <Activity />
          </div>
          <CurrentTask />
          <LearningQueue />
        </div>
      </div>

      <Inspector />
      <Proactive />
      <ToolsOverlay />
      <SchedulesOverlay />
      <ConfirmModal />
    </div>
  )
}
