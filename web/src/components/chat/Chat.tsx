import { useEffect, useRef, useState } from 'react'

import type { ChatMessage, ToolCall } from '../../lib/types'
import { useStore } from '../../stores/store'
import { chatSocket } from '../../websocket/useSaliStream'
import { Dot, Empty, Panel, PanelHeader } from '../ui/primitives'

function ToolLine({ t }: { t: ToolCall }): JSX.Element {
  const running = t.phase === 'start'
  const color = running ? 'var(--warn)' : t.ok === false ? 'var(--bad)' : 'var(--ok)'
  return (
    <div className="flex items-start gap-2 py-0.5 font-mono text-2xs text-dim">
      <span className="mt-0.5">
        <Dot color={color} pulse={running} />
      </span>
      <span className="text-ink/80">{t.name}</span>
      <span className="truncate text-faint">{running ? 'running…' : (t.summary ?? (t.ok ? 'ok' : 'failed'))}</span>
    </div>
  )
}

function Message({ m }: { m: ChatMessage }): JSX.Element {
  if (m.role === 'user') {
    return (
      <div className="animate-fade-in">
        <div className="panel-label mb-1 text-accent-dim">Almir</div>
        <div className="whitespace-pre-wrap rounded-md border border-line bg-raised/50 px-3 py-2 text-sm text-ink">
          {m.text}
        </div>
      </div>
    )
  }
  return (
    <div className="animate-fade-in">
      <div className="panel-label mb-1" style={{ color: 'var(--accent)' }}>
        Sali
      </div>
      {m.thinking && (
        <div className="mb-1 border-l border-line pl-2 font-mono text-2xs italic text-faint">
          {m.thinking.slice(-240)}
        </div>
      )}
      {m.tools.length > 0 && (
        <div className="mb-1.5 rounded-md border border-line bg-base/40 px-2 py-1">
          {m.tools.map((t, i) => (
            <ToolLine key={i} t={t} />
          ))}
        </div>
      )}
      <div className="whitespace-pre-wrap text-sm leading-relaxed text-ink">
        {m.text}
        {m.streaming && <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse-soft bg-accent align-middle" />}
      </div>
    </div>
  )
}

export function Chat(): JSX.Element {
  const chat = useStore((s) => s.chat)
  const sensing = useStore((s) => s.sensing)
  const pushUser = useStore((s) => s.pushUser)
  const [draft, setDraft] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [chat])

  const send = () => {
    const text = draft.trim()
    if (!text) return
    pushUser(text)
    chatSocket.message(text)
    setDraft('')
  }

  return (
    <Panel>
      <PanelHeader label="Conversation" id="chat" right={<span className="font-mono text-2xs text-faint">one continuous session</span>} />
      <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto px-3 py-3">
        {chat.length === 0 ? (
          <Empty>Sali is present. Ask anything — the same session as the terminal.</Empty>
        ) : (
          chat.map((m) => <Message key={m.id} m={m} />)
        )}
      </div>
      <div className="shrink-0 border-t border-line px-3 py-2.5">
        {sensing && (
          <div className="mb-1.5 flex items-center gap-2 font-mono text-2xs text-faint">
            <Dot color="var(--interesting)" pulse /> <span className="italic">noticing: {sensing}</span>
          </div>
        )}
        <div className="flex items-end gap-2">
          <textarea
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value)
              chatSocket.typing(e.target.value)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                send()
              }
            }}
            rows={1}
            aria-label="Message Sali"
            placeholder="Message Sali…"
            className="max-h-28 min-h-[38px] flex-1 resize-none rounded-md border border-line bg-base/60 px-3 py-2 text-sm text-ink outline-none placeholder:text-faint focus:border-line-strong"
          />
          <button
            onClick={send}
            className="rounded-md border border-accent-dim bg-accent/10 px-3 py-2 font-mono text-2xs uppercase tracking-widest text-accent transition-colors hover:bg-accent/20"
          >
            Send
          </button>
        </div>
      </div>
    </Panel>
  )
}
