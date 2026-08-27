import { useEffect } from 'react'

import type { LoopEvent } from '../lib/types'
import { useStore, type ConnState } from '../stores/store'

// The chat turn stream — the SAME continuous conversation as the terminal (one persistent session on the
// backend). A module singleton so any component can send without prop-drilling; the hook wires its events
// into the store. Auto-reconnects; the environmental firehose keeps flowing regardless (§19).

function wsUrl(path: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}${path}`
}

class ChatSocket {
  private ws: WebSocket | null = null
  private closed = false
  private backoff = 500
  private onEvent: (ev: LoopEvent) => void = () => {}
  private onState: (s: ConnState) => void = () => {}

  start(onEvent: (ev: LoopEvent) => void, onState: (s: ConnState) => void): void {
    this.onEvent = onEvent
    this.onState = onState
    this.closed = false
    this.connect()
  }

  private connect(): void {
    if (this.closed) return
    this.onState('connecting')
    this.ws = new WebSocket(wsUrl('/ws'))
    this.ws.onopen = () => {
      this.backoff = 500
      this.onState('open')
    }
    this.ws.onmessage = (msg) => {
      try {
        this.onEvent(JSON.parse(msg.data as string) as LoopEvent)
      } catch {
        /* ignore a malformed frame */
      }
    }
    this.ws.onclose = () => {
      this.onState('closed')
      if (!this.closed) {
        this.backoff = Math.min(this.backoff * 1.6, 8000)
        setTimeout(() => this.connect(), this.backoff)
      }
    }
    this.ws.onerror = () => this.ws?.close()
  }

  private send(obj: unknown): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj))
  }

  message(text: string): void {
    this.send({ type: 'message', text })
  }
  typing(text: string): void {
    this.send({ type: 'typing', text })
  }
  confirm(id: string, ok: boolean): void {
    this.send({ type: 'confirm_response', id, ok })
  }

  stop(): void {
    this.closed = true
    this.ws?.close()
  }
}

export const chatSocket = new ChatSocket()

export function useSaliStream(): void {
  const applyLoop = useStore((s) => s.applyLoop)
  const setChatState = useStore((s) => s.setChatState)
  useEffect(() => {
    chatSocket.start(applyLoop, setChatState)
    return () => chatSocket.stop()
  }, [applyLoop, setChatState])
}
