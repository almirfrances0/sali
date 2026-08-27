import { useLayoutEffect, useRef, useState } from 'react'
import GridLayout, { WidthProvider, type Layout } from 'react-grid-layout'

import 'react-grid-layout/css/styles.css'
import 'react-resizable/css/styles.css'

import { useStore } from '../../stores/store'
import { Activity } from '../activity/Activity'
import { Attention } from '../attention/Attention'
import { Brain } from '../brain/Brain'
import { Chat } from '../chat/Chat'
import { World } from '../world/World'
import { Tasks } from '../tasks/Tasks'

const Grid = WidthProvider(GridLayout)

const COLS = 24
const ROWS = 12
const MARGIN = 8
const LS_KEY = 'sali-cockpit-layout-v2'

// The default cockpit — the brain dominant at centre, world/attention left, activity/tasks middle-right,
// the conversation right. The user can drag (by a panel's header), resize (bottom-right), and collapse
// (the ▾ chevron) any widget; the arrangement persists. Extendable — grow one and the screen scrolls.
const DEFAULT: Layout[] = [
  { i: 'world', x: 0, y: 0, w: 4, h: 8, minW: 3, minH: 3 },
  { i: 'attention', x: 0, y: 8, w: 4, h: 4, minW: 3, minH: 2 },
  { i: 'brain', x: 4, y: 0, w: 11, h: 12, minW: 6, minH: 5 },
  { i: 'activity', x: 15, y: 0, w: 4, h: 7, minW: 3, minH: 3 },
  { i: 'tasks', x: 15, y: 7, w: 4, h: 5, minW: 3, minH: 2 },
  { i: 'chat', x: 19, y: 0, w: 5, h: 12, minW: 3, minH: 4 },
]

function loadLayout(): Layout[] {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (raw) {
      const saved = JSON.parse(raw) as Layout[]
      // keep any newly-added widgets that aren't in the saved layout
      const byId = new Map(saved.map((l) => [l.i, l]))
      return DEFAULT.map((d) => ({ ...d, ...(byId.get(d.i) ?? {}) }))
    }
  } catch {
    /* fall through to default */
  }
  return DEFAULT
}

const WIDGETS: Record<string, JSX.Element> = {
  world: <World />,
  attention: <Attention />,
  brain: <Brain />,
  activity: <Activity />,
  tasks: <Tasks />,
  chat: <Chat />,
}

export function Cockpit(): JSX.Element {
  const collapsed = useStore((s) => s.collapsed)
  const [layout, setLayout] = useState<Layout[]>(loadLayout)
  const [rowHeight, setRowHeight] = useState(80)
  const containerRef = useRef<HTMLDivElement>(null)

  // Fit ROWS rows into the available height so the default cockpit needs no scroll (§29).
  useLayoutEffect(() => {
    const el = containerRef.current
    if (!el) return
    const measure = () => {
      const h = el.clientHeight
      setRowHeight(Math.max(40, Math.floor((h - (ROWS + 1) * MARGIN) / ROWS)))
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const persist = (next: Layout[]) => {
    // never persist the temporary collapsed height (h=1) — keep each widget's expanded height
    const merged = next.map((n) => {
      if (collapsed[n.i]) {
        const prev = layout.find((l) => l.i === n.i)
        return { ...n, h: prev ? prev.h : n.h }
      }
      return n
    })
    setLayout(merged)
    try {
      localStorage.setItem(LS_KEY, JSON.stringify(merged))
    } catch {
      /* storage may be unavailable — the layout just won't persist */
    }
  }

  const effective = layout.map((l) => (collapsed[l.i] ? { ...l, h: 1, isResizable: false } : l))

  const reset = () => {
    try {
      localStorage.removeItem(LS_KEY)
    } catch {
      /* ignore */
    }
    setLayout(DEFAULT)
    useStore.setState({ collapsed: {} })
  }

  return (
    <div ref={containerRef} className="relative min-h-0 flex-1 overflow-auto">
      <Grid
        className="layout"
        layout={effective}
        cols={COLS}
        rowHeight={rowHeight}
        margin={[MARGIN, MARGIN]}
        containerPadding={[MARGIN, MARGIN]}
        draggableHandle=".panel-drag"
        compactType={null}
        preventCollision={false}
        isBounded={false}
        onLayoutChange={persist}
        resizeHandles={['se']}
      >
        {layout.map((l) => (
          <div key={l.i}>{WIDGETS[l.i]}</div>
        ))}
      </Grid>
      <button
        onClick={reset}
        className="fixed bottom-2 left-2 z-20 rounded border border-line bg-panel/80 px-2 py-1 font-mono text-2xs text-faint backdrop-blur hover:text-ink"
        title="reset the cockpit layout"
      >
        ⟲ layout
      </button>
    </div>
  )
}
