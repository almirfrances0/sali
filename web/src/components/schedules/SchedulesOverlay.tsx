import { useEffect } from 'react'

import { useStore } from '../../stores/store'
import { Schedules } from './Schedules'

// Schedules open as a focused overlay (click-to-focus, §14) — the brain stays the primary experience.
export function SchedulesOverlay(): JSX.Element | null {
  const open = useStore((s) => s.schedulesOpen)
  const setOpen = useStore((s) => s.setSchedulesOpen)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [setOpen])
  if (!open) return null
  return (
    <div className="absolute inset-0 z-50 flex items-center justify-center bg-base/50 p-6 backdrop-blur-sm" onClick={() => setOpen(false)}>
      <div className="h-[70%] w-[min(460px,92%)] animate-fade-in" onClick={(e) => e.stopPropagation()}>
        <Schedules />
      </div>
    </div>
  )
}
