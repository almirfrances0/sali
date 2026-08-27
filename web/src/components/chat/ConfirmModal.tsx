import { useStore } from '../../stores/store'
import { chatSocket } from '../../websocket/useSaliStream'

// The destructive-action gate (§34). The backend policy decides what needs confirming and sends a
// `confirm` frame over /ws; the browser only renders the choice and replies — it can never widen authority.
export function ConfirmModal(): JSX.Element | null {
  const confirm = useStore((s) => s.confirm)
  const resolve = useStore((s) => s.resolveConfirm)
  if (!confirm) return null

  const answer = (ok: boolean) => {
    chatSocket.confirm(confirm.id, ok)
    resolve()
  }

  return (
    <div className="absolute inset-0 z-50 flex items-center justify-center bg-base/60 backdrop-blur-sm">
      <div className="w-[min(520px,90%)] animate-fade-in rounded-lg border border-line-strong bg-panel p-4 shadow-panel">
        <div className="panel-label mb-2 text-important">confirm action</div>
        <div className="mb-1 font-mono text-xs text-accent">{confirm.tool}</div>
        <div className="mb-3 text-sm text-ink">{confirm.reason}</div>
        {Object.keys(confirm.args).length > 0 && (
          <pre className="mb-3 max-h-40 overflow-auto rounded-md border border-line bg-base/50 p-2 font-mono text-2xs text-dim">
            {JSON.stringify(confirm.args, null, 2)}
          </pre>
        )}
        <div className="flex justify-end gap-2">
          <button
            onClick={() => answer(false)}
            className="rounded border border-line px-3 py-1.5 font-mono text-2xs uppercase tracking-wider text-dim hover:border-line-strong"
          >
            Deny
          </button>
          <button
            onClick={() => answer(true)}
            className="rounded border border-important bg-important/10 px-3 py-1.5 font-mono text-2xs uppercase tracking-wider text-important hover:bg-important/20"
          >
            Approve
          </button>
        </div>
      </div>
    </div>
  )
}
