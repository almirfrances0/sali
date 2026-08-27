import type { ReactNode } from 'react'

// Small, quiet building blocks so every panel reads as one system.

export function Panel({
  area,
  className = '',
  children,
}: {
  area?: string
  className?: string
  children: ReactNode
}): JSX.Element {
  return <section className={`panel flex min-h-0 flex-col overflow-hidden ${area ?? ''} ${className}`}>{children}</section>
}

export function PanelHeader({
  label,
  right,
}: {
  label: string
  right?: ReactNode
}): JSX.Element {
  return (
    <header className="flex shrink-0 items-center justify-between border-b border-line px-3 py-2">
      <span className="panel-label">{label}</span>
      {right}
    </header>
  )
}

export function Dot({ color, pulse = false }: { color: string; pulse?: boolean }): JSX.Element {
  return (
    <span
      className={`inline-block h-2 w-2 shrink-0 rounded-full ${pulse ? 'animate-pulse-soft' : ''}`}
      style={{ background: color, boxShadow: `0 0 8px ${color}` }}
    />
  )
}

export function Chip({ children, title }: { children: ReactNode; title?: string }): JSX.Element {
  return (
    <span className="chip" title={title}>
      {children}
    </span>
  )
}

export function Empty({ children }: { children: ReactNode }): JSX.Element {
  return <div className="flex h-full items-center justify-center px-4 text-center text-2xs text-faint">{children}</div>
}

export function riskColor(risk: number): string {
  if (risk >= 4) return 'var(--critical)'
  if (risk >= 3) return 'var(--important)'
  if (risk >= 2) return 'var(--interesting)'
  return 'var(--routine)'
}

// A compact relative time ("12s", "4m", "2h") from an ISO string or epoch ms.
export function ago(at: string | number): string {
  const t = typeof at === 'number' ? at : Date.parse(at)
  const s = Math.max(0, (Date.now() - t) / 1000)
  if (s < 60) return `${Math.floor(s)}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}
