import { Activity } from '../activity/Activity'
import { Attention } from '../attention/Attention'
import { Schedules } from '../schedules/Schedules'
import { Tasks } from '../tasks/Tasks'
import { ToolsExec } from '../tools/ToolsExec'
import { World } from '../world/World'
import { useActiveWidgets, type WidgetId } from '../../hooks/useActiveWidgets'
import { useStore } from '../../stores/store'

// The process rail — Sali raises a widget here ONLY while it is actually doing that thing (or the user
// pinned it). When nothing is active the rail is empty and the brain expands to fill (§30/§37). Order is
// fixed so widgets don't jump around as they come and go.
const ORDER: WidgetId[] = ['tools', 'tasks', 'world', 'attention', 'activity', 'schedules']
const COMPONENT: Record<WidgetId, () => JSX.Element> = {
  world: World,
  tasks: Tasks,
  tools: ToolsExec,
  activity: Activity,
  attention: Attention,
  schedules: Schedules,
}

export function ProcessRail(): JSX.Element | null {
  const active = useActiveWidgets()
  const collapsed = useStore((s) => s.collapsed)
  const show = ORDER.filter((w) => active.has(w))
  if (show.length === 0) return null
  return (
    <div className="flex w-[clamp(300px,22%,400px)] shrink-0 animate-fade-in flex-col gap-2.5 overflow-y-auto">
      {show.map((w) => {
        const Widget = COMPONENT[w]
        // a collapsed widget shows only its header (its ▾ chevron), so the rail stays compact
        return (
          <div key={w} className={collapsed[w] ? 'h-9 shrink-0' : 'min-h-[190px] flex-1'}>
            <Widget />
          </div>
        )
      })}
    </div>
  )
}
