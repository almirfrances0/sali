import { Html, Line, OrbitControls } from '@react-three/drei'
import { Canvas, useFrame } from '@react-three/fiber'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef } from 'react'
import type { Mesh, MeshStandardMaterial } from 'three'

import { api } from '../../lib/api'
import type { GraphEdge, GraphNode } from '../../lib/types'
import { useStore } from '../../stores/store'
import { Empty, Panel, PanelHeader } from '../ui/primitives'
import { layout, nodeColor, nodeRadius, prefersReducedMotion, type Vec3 } from './graph'

const HUB_TYPES = new Set(['agent', 'machine', 'person', 'model', 'service', 'project'])
const ACTIVE_MS = 2600 // how long a retrieved node/edge stays lit after a real retrieval
const SIG_DUR = 1600 // ms for one electrical signal to travel a synapse
const SIG_MAX = 160 // hard cap on concurrent signals (backpressure)

// ── a neuron: a glowing cell + a soft halo; the agent cell breathes while Sali is actually working ──
function NodeMesh({
  node,
  position,
  isActiveRef,
  selected,
  dim,
  pulsing,
  onSelect,
}: {
  node: GraphNode
  position: Vec3
  isActiveRef: { current: (id: string) => number }
  selected: boolean
  dim: boolean
  pulsing: boolean
  onSelect: (n: GraphNode) => void
}): JSX.Element {
  const ref = useRef<Mesh>(null)
  const halo = useRef<Mesh>(null)
  const color = nodeColor(node.node_type)
  const r = nodeRadius(node)
  useFrame(({ clock }) => {
    const m = ref.current
    if (!m) return
    const act = isActiveRef.current(node.id) // 0..1 recent-activation intensity (real retrieval)
    const boost = selected ? 1 : 0
    // the agent neuron breathes only when presence is genuinely active — real state, not a timer
    const breathe = pulsing ? (Math.sin(clock.elapsedTime * 3) * 0.5 + 0.5) * 0.35 : 0
    const target = (1 + act * 0.55 + boost * 0.35 + breathe) * (dim ? 0.6 : 1)
    m.scale.setScalar(m.scale.x + (target - m.scale.x) * 0.2)
    const mat = m.material as MeshStandardMaterial
    mat.emissiveIntensity = (0.4 + act * 2.6 + boost * 0.6 + breathe * 1.4) * (dim ? 0.18 : 1)
    mat.opacity += ((dim ? 0.28 : 1) - mat.opacity) * 0.2
    if (halo.current) {
      const hm = halo.current.material as MeshStandardMaterial
      hm.opacity = (0.06 + act * 0.22 + breathe * 0.18) * (dim ? 0.3 : 1)
      halo.current.scale.setScalar(m.scale.x)
    }
  })
  return (
    <group position={position}>
      <mesh ref={halo} scale={1}>
        <sphereGeometry args={[r * 2.1, 16, 16]} />
        <meshStandardMaterial color={color} emissive={color} emissiveIntensity={1} transparent opacity={0.08} depthWrite={false} />
      </mesh>
      <mesh
        ref={ref}
        onClick={(e) => {
          e.stopPropagation()
          onSelect(node)
        }}
        onPointerOver={(e) => {
          e.stopPropagation()
          document.body.style.cursor = 'pointer'
        }}
        onPointerOut={() => {
          document.body.style.cursor = 'auto'
        }}
      >
        <sphereGeometry args={[r, 20, 20]} />
        <meshStandardMaterial color={color} emissive={color} emissiveIntensity={0.4} roughness={0.35} metalness={0.1} transparent />
      </mesh>
    </group>
  )
}

// ── a synapse: a thin dendrite; brightens while carrying real retrieval activity ──
function EdgeLine({ a, b, activeRef, dim }: { a: Vec3; b: Vec3; activeRef: { current: number }; dim: boolean }): JSX.Element {
  const ref = useRef<any>(null)
  useFrame(() => {
    const l = ref.current
    if (!l) return
    const k = activeRef.current
    l.material.opacity = (0.1 + k * 0.7) * (dim ? 0.22 : 1)
    l.material.color.setRGB(0.4 + k * 0.1, 0.55 + k * 0.35, 0.62 + k * 0.3)
  })
  return <Line ref={ref} points={[a, b]} color="#5f7d92" lineWidth={1} transparent opacity={0.1} />
}

// ── the electrical activity: bright pulses travelling real synapses, spawned by REAL events ──
// Retrieval frames light the specific synapses a turn used; every durable event fires one pulse along a
// real synapse of the hub. The COUNT and TIMING are real — the brain is busy exactly when Sali is busy,
// and quiet when Sali is quiet (§7/§23/§37). Nothing loops on a timer.
interface Sig {
  a: Vec3
  b: Vec3
  start: number
}
function Signals({
  edges,
  positions,
  activeEdgeIdx,
  activeAt,
}: {
  edges: GraphEdge[]
  positions: Map<string, Vec3>
  activeEdgeIdx: number[]
  activeAt: number
}): JSX.Element {
  const refs = useRef<(Mesh | null)[]>([])
  const pool = useRef<Sig[]>([])
  const lastSeq = useStore((s) => s.lastSeq)
  const prevSeq = useRef(0)

  const push = (idx: number, offset = 0) => {
    const e = edges[idx]
    if (!e) return
    const a = positions.get(e.src_id)
    const b = positions.get(e.dst_id)
    if (a && b && pool.current.length < SIG_MAX) pool.current.push({ a, b, start: performance.now() + offset })
  }

  // real retrieval → fire a short train of pulses along the exact synapses that turn used
  useEffect(() => {
    for (const i of activeEdgeIdx) {
      push(i, 0)
      push(i, 220)
      push(i, 440)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeAt])

  // every durable event → a pulse along a real synapse somewhere in the network; a burst of N events
  // fires ~N pulses (seq is monotonic, so the delta IS the real event count — the brain is as busy as
  // Sali is, quiet when Sali is quiet). Deterministic by seq, spread across the graph — never random.
  useEffect(() => {
    const delta = Math.min(Math.max(lastSeq - prevSeq.current, 1), 8)
    prevSeq.current = lastSeq
    if (edges.length) for (let k = 0; k < delta; k++) push((lastSeq + k * 17) % edges.length)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastSeq])

  useFrame(() => {
    const now = performance.now()
    pool.current = pool.current.filter((s) => now - s.start < SIG_DUR)
    for (let i = 0; i < SIG_MAX; i++) {
      const m = refs.current[i]
      if (!m) continue
      const s = pool.current[i]
      const t = s ? (now - s.start) / SIG_DUR : -1
      if (s && t >= 0) {
        const e = t * t * (3 - 2 * t) // smoothstep along the synapse
        m.position.set(s.a[0] + (s.b[0] - s.a[0]) * e, s.a[1] + (s.b[1] - s.a[1]) * e, s.a[2] + (s.b[2] - s.a[2]) * e)
        m.scale.setScalar(0.11 + Math.sin(t * Math.PI) * 0.26)
        m.visible = true
      } else {
        m.visible = false // not started yet (future offset) or empty slot
      }
    }
  })

  return (
    <>
      {Array.from({ length: SIG_MAX }, (_, i) => (
        <mesh
          key={i}
          ref={(el) => {
            refs.current[i] = el
          }}
          visible={false}
        >
          <sphereGeometry args={[1, 12, 12]} />
          <meshBasicMaterial color="#a6fbee" toneMapped={false} transparent opacity={0.95} />
        </mesh>
      ))}
    </>
  )
}

function Scene({ nodes, edges }: { nodes: GraphNode[]; edges: GraphEdge[] }): JSX.Element {
  const positions = useMemo(() => layout(nodes, edges), [nodes, edges])
  const nameToId = useMemo(() => {
    const m = new Map<string, string>()
    for (const n of nodes) m.set(n.name.toLowerCase(), n.id)
    return m
  }, [nodes])

  const activation = useStore((s) => s.activation)
  const selection = useStore((s) => s.selection)
  const select = useStore((s) => s.select)
  const presence = useStore((s) => s.presence?.presence)
  const pulsingHub = presence === 'thinking' || presence === 'executing' || presence === 'learning'

  // the central neuron = Sali's agent node (fallback: the highest-degree hub)
  const hubId = useMemo(() => {
    const agent = nodes.find((n) => n.node_type === 'agent')
    if (agent) return agent.id
    const deg = new Map<string, number>()
    for (const e of edges) {
      deg.set(e.src_id, (deg.get(e.src_id) ?? 0) + 1)
      deg.set(e.dst_id, (deg.get(e.dst_id) ?? 0) + 1)
    }
    return [...deg.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null
  }, [nodes, edges])

  const activeNodeIds = useMemo(() => {
    const s = new Set<string>()
    if (activation) {
      for (const f of activation.graph) {
        const a = nameToId.get(f.src.toLowerCase())
        const b = nameToId.get(f.dst.toLowerCase())
        if (a) s.add(a)
        if (b) s.add(b)
      }
    }
    return s
  }, [activation, nameToId])
  const activeEdgeIdx = useMemo(
    () => edges.map((e, i) => (activeNodeIds.has(e.src_id) && activeNodeIds.has(e.dst_id) ? i : -1)).filter((i) => i >= 0),
    [edges, activeNodeIds],
  )

  const focusId = selection?.kind === 'node' ? selection.id : null
  const neighborIds = useMemo(() => {
    if (!focusId) return null
    const s = new Set<string>([focusId])
    for (const e of edges) {
      if (e.src_id === focusId) s.add(e.dst_id)
      if (e.dst_id === focusId) s.add(e.src_id)
    }
    return s
  }, [focusId, edges])

  const activeAt = activation?.at ?? 0
  const intensity = () => Math.max(0, 1 - (Date.now() - activeAt) / ACTIVE_MS)
  const nodeActiveRef = useRef((id: string) => (activeNodeIds.has(id) ? intensity() : 0))
  nodeActiveRef.current = (id: string) => (activeNodeIds.has(id) ? intensity() : 0)

  const edgeActive = useMemo(
    () => edges.map((e) => ({ ref: { current: 0 }, lit: activeNodeIds.has(e.src_id) && activeNodeIds.has(e.dst_id) })),
    [edges, activeNodeIds],
  )
  useFrame(() => {
    const k = intensity()
    for (const e of edgeActive) e.ref.current = e.lit ? k : 0
  })

  return (
    <>
      <ambientLight intensity={0.5} />
      <pointLight position={[8, 10, 12]} intensity={0.7} />
      <pointLight position={[-10, -6, -8]} intensity={0.25} color="#4fe0cf" />
      {edges.map((e, i) => {
        const a = positions.get(e.src_id)
        const b = positions.get(e.dst_id)
        if (!a || !b) return null
        const edgeDim = focusId != null && e.src_id !== focusId && e.dst_id !== focusId
        return <EdgeLine key={e.id} a={a} b={b} activeRef={edgeActive[i].ref} dim={edgeDim} />
      })}
      <Signals edges={edges} positions={positions} activeEdgeIdx={activeEdgeIdx} activeAt={activeAt} />
      {nodes.map((n) => {
        const p = positions.get(n.id)
        if (!p) return null
        return (
          <NodeMesh
            key={n.id}
            node={n}
            position={p}
            isActiveRef={nodeActiveRef}
            selected={focusId === n.id}
            dim={neighborIds != null && !neighborIds.has(n.id)}
            pulsing={pulsingHub && n.id === hubId}
            onSelect={(node) => select({ kind: 'node', id: node.id, label: node.name })}
          />
        )
      })}
      {nodes
        .filter((n) => HUB_TYPES.has(n.node_type))
        .map((n) => {
          const p = positions.get(n.id)
          if (!p) return null
          return (
            <Html key={`l-${n.id}`} position={[p[0], p[1] + nodeRadius(n) + 0.25, p[2]]} center distanceFactor={12} style={{ pointerEvents: 'none' }}>
              <div className="whitespace-nowrap font-mono text-[10px] tracking-wide text-ink/80">{n.name}</div>
            </Html>
          )
        })}
      <OrbitControls enablePan={false} enableDamping dampingFactor={0.08} minDistance={6} maxDistance={34} autoRotate={!prefersReducedMotion()} autoRotateSpeed={0.24} />
    </>
  )
}

export function Brain(): JSX.Element {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['graph-snapshot'],
    queryFn: () => api.graphSnapshot(220),
    staleTime: 30_000,
  })
  const setToolsOpen = useStore((s) => s.setToolsOpen)
  const collapsed = data?.collapsed ?? {}
  const collapsedCount = Object.values(collapsed).reduce((a, b) => a + b, 0)

  return (
    <Panel>
      <PanelHeader
        label="Neural memory"
        id="brain"
        right={
          data ? (
            <span className="font-mono text-2xs text-faint">
              {data.nodes.length} neurons · {data.edges.length} synapses
              {collapsedCount > 0 && (
                <>
                  {' · '}
                  <button onClick={() => setToolsOpen(true)} onMouseDown={(e) => e.stopPropagation()} className="text-accent-dim hover:text-accent">
                    {collapsedCount} tools ▸
                  </button>
                </>
              )}
            </span>
          ) : null
        }
      />
      <div
        className="relative min-h-0 flex-1"
        role="img"
        aria-label={
          data
            ? `Sali's knowledge graph: ${data.nodes.length} entities, ${data.edges.length} relationships. Entity details are also in the World panel and by selecting a node.`
            : 'Sali knowledge graph, loading'
        }
      >
        <div className="pointer-events-none absolute inset-0 z-10 vignette" />
        {isLoading && <Empty>waking the graph…</Empty>}
        {isError && <Empty>the graph is unreachable — is `sali serve` running?</Empty>}
        {data && (
          <Canvas camera={{ position: [0, 1.5, 17], fov: 50 }} dpr={[1, 2]} gl={{ antialias: true, alpha: true }}>
            <Scene nodes={data.nodes} edges={data.edges} />
          </Canvas>
        )}
      </div>
    </Panel>
  )
}
