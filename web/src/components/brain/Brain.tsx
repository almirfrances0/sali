import { Html, Line, OrbitControls } from '@react-three/drei'
import { Canvas, useFrame } from '@react-three/fiber'
import { useQuery } from '@tanstack/react-query'
import { useMemo, useRef } from 'react'
import type { Mesh, MeshStandardMaterial } from 'three'

import { api } from '../../lib/api'
import type { GraphEdge, GraphNode } from '../../lib/types'
import { useStore } from '../../stores/store'
import { Empty, Panel, PanelHeader } from '../ui/primitives'
import { layout, nodeColor, nodeRadius, prefersReducedMotion, type Vec3 } from './graph'

const HUB_TYPES = new Set(['agent', 'machine', 'person', 'model', 'service', 'project'])
const ACTIVE_MS = 2600 // how long a retrieved node/edge stays lit after a real retrieval

function NodeMesh({
  node,
  position,
  isActiveRef,
  selected,
  dim,
  onSelect,
}: {
  node: GraphNode
  position: Vec3
  isActiveRef: { current: (id: string) => number }
  selected: boolean
  dim: boolean
  onSelect: (n: GraphNode) => void
}): JSX.Element {
  const ref = useRef<Mesh>(null)
  const color = nodeColor(node.node_type)
  const r = nodeRadius(node)
  useFrame(() => {
    const m = ref.current
    if (!m) return
    const act = isActiveRef.current(node.id) // 0..1 recent-activation intensity
    const boost = selected ? 1 : 0
    const target = (1 + act * 0.55 + boost * 0.35) * (dim ? 0.6 : 1)
    m.scale.setScalar(m.scale.x + (target - m.scale.x) * 0.2)
    const mat = m.material as MeshStandardMaterial
    mat.emissiveIntensity = (0.35 + act * 2.4 + boost * 0.6) * (dim ? 0.18 : 1)
    mat.opacity += ((dim ? 0.28 : 1) - mat.opacity) * 0.2
  })
  return (
    <mesh
      ref={ref}
      position={position}
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
      <meshStandardMaterial
        color={color}
        emissive={color}
        emissiveIntensity={0.35}
        roughness={0.35}
        metalness={0.1}
        transparent
      />
    </mesh>
  )
}

function EdgeLine({
  a,
  b,
  activeRef,
  dim,
}: {
  a: Vec3
  b: Vec3
  activeRef: { current: number }
  dim: boolean
}): JSX.Element {
  const ref = useRef<any>(null)
  useFrame(() => {
    const l = ref.current
    if (!l) return
    const k = activeRef.current
    l.material.opacity = (0.12 + k * 0.7) * (dim ? 0.22 : 1)
    l.material.color.setRGB(0.42 + k * 0.05, 0.55 + k * 0.35, 0.62 + k * 0.28)
  })
  return <Line ref={ref} points={[a, b]} color="#6a8398" lineWidth={1} transparent opacity={0.12} />
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

  // Which nodes/edges the LAST real retrieval touched (matched by the graph-fact endpoint names). This is
  // real brain activity: a genuine memory_search lights the genuine nodes it used — never a timer.
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

  // Focus (§11): selecting a node dims everything that isn't it or its direct neighbours, so a click
  // reveals that entity's neighbourhood in place — no page nav, nothing disappears.
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

  // per-edge activation ref objects, updated each frame from the same decay
  const edgeActive = useMemo(
    () =>
      edges.map((e) => ({
        ref: { current: 0 },
        lit: activeNodeIds.has(e.src_id) && activeNodeIds.has(e.dst_id),
      })),
    [edges, activeNodeIds],
  )
  useFrame(() => {
    const k = intensity()
    for (const e of edgeActive) e.ref.current = e.lit ? k : 0
  })

  return (
    <>
      <ambientLight intensity={0.55} />
      <pointLight position={[8, 10, 12]} intensity={0.7} />
      <pointLight position={[-10, -6, -8]} intensity={0.25} color="#4fe0cf" />
      {edges.map((e, i) => {
        const a = positions.get(e.src_id)
        const b = positions.get(e.dst_id)
        if (!a || !b) return null
        const edgeDim = focusId != null && e.src_id !== focusId && e.dst_id !== focusId
        return <EdgeLine key={e.id} a={a} b={b} activeRef={edgeActive[i].ref} dim={edgeDim} />
      })}
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
            <Html
              key={`l-${n.id}`}
              position={[p[0], p[1] + nodeRadius(n) + 0.25, p[2]]}
              center
              distanceFactor={12}
              style={{ pointerEvents: 'none' }}
            >
              <div className="whitespace-nowrap font-mono text-[10px] tracking-wide text-ink/80">{n.name}</div>
            </Html>
          )
        })}
      <OrbitControls
        enablePan={false}
        enableDamping
        dampingFactor={0.08}
        minDistance={6}
        maxDistance={34}
        autoRotate={!prefersReducedMotion()}
        autoRotateSpeed={0.28}
      />
    </>
  )
}

export function Brain(): JSX.Element {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['graph-snapshot'],
    queryFn: () => api.graphSnapshot(220),
    staleTime: 30_000,
  })
  const collapsed = data?.collapsed ?? {}
  const collapsedCount = Object.values(collapsed).reduce((a, b) => a + b, 0)

  return (
    <Panel area="area-brain">
      <PanelHeader
        label="Neural memory"
        right={
          data ? (
            <span className="font-mono text-2xs text-faint">
              {data.nodes.length} entities · {data.edges.length} links
              {collapsedCount > 0 ? ` · ${collapsedCount} tools clustered` : ''}
            </span>
          ) : null
        }
      />
      <div
        className="relative min-h-0 flex-1"
        role="img"
        aria-label={
          data
            ? `Sali's knowledge graph: ${data.nodes.length} entities, ${data.edges.length} relationships. ` +
              `Entity details are also available in the World panel and by selecting a node.`
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
