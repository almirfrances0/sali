import { useEffect, useRef } from 'react'

import type { SaliEvent } from '../lib/types'
import { useStore } from '../stores/store'

// The live firehose. Opens one WebSocket to /stream, batches incoming frames per animation frame (so a
// burst of events is one render, not N — §33), and reconnects with backoff. On RECONNECT it resumes from
// the last seq it saw (?since=) so nothing is missed; the first connect is live-only (quiet until Sali
// actually does something — §37), which is honest, not empty-looking-by-accident.

function wsUrl(path: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}${path}`
}

export function useEventStream(): void {
  const ingest = useStore((s) => s.ingestEvents)
  const setStreamState = useStore((s) => s.setStreamState)
  const bufRef = useRef<SaliEvent[]>([])
  const rafRef = useRef<number | null>(null)
  const connectedOnceRef = useRef(false)

  useEffect(() => {
    let ws: WebSocket | null = null
    let closed = false
    let backoff = 500

    const flush = () => {
      rafRef.current = null
      if (bufRef.current.length) {
        ingest(bufRef.current)
        bufRef.current = []
      }
    }

    const connect = () => {
      if (closed) return
      const since = connectedOnceRef.current ? useStore.getState().lastSeq : undefined
      const url = wsUrl('/stream') + (since ? `?since=${since}` : '')
      setStreamState('connecting')
      ws = new WebSocket(url)

      ws.onopen = () => {
        connectedOnceRef.current = true
        backoff = 500
        setStreamState('open')
      }
      ws.onmessage = (msg) => {
        try {
          const ev = JSON.parse(msg.data as string) as SaliEvent
          bufRef.current.push(ev)
          if (rafRef.current == null) rafRef.current = requestAnimationFrame(flush)
        } catch {
          /* a malformed frame is dropped, never crashes the stream */
        }
      }
      ws.onclose = () => {
        setStreamState('closed')
        if (!closed) {
          backoff = Math.min(backoff * 1.6, 8000)
          setTimeout(connect, backoff)
        }
      }
      ws.onerror = () => ws?.close()
    }

    connect()
    return () => {
      closed = true
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current)
      ws?.close()
    }
  }, [ingest, setStreamState])
}
