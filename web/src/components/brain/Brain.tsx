import { Html, Line, OrbitControls } from '@react-three/drei'
import { Canvas, useFrame } from '@react-three/fiber'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef } from 'react'
import { BufferAttribute, BufferGeometry, type Mesh, type MeshStandardMaterial } from 'three'

import { api } from '../../lib/api'
import type { GraphEdge, GraphNode } from '../../lib/types'
import { useStore } from '../../stores/store'
import { Empty, Panel, PanelHeader } from '../ui/primitives'
import { layout, nodeColor, nodeRadius, prefersReducedMotion, type Vec3 } from './graph'

const HUB_TYPES = new Set(['agent', 'machine', 'person', 'model', 'service', 'project'])
// Firing dynamics (a small, bounded neural simulation — SEEDED by real events/retrievals, never a timer).
const THRESHOLD = 1.0 // charge at which a neuron fires an action potential
const REFRACTORY = 260 // ms a neuron cannot re-fire (real refractory period → bounds cascades)
const FLASH = 360 // ms the soma stays lit after firing (the spike)
const AP_MS = 520 // ms an action potential takes to travel a synapse
const ARRIVE = 0.6 // charge delivered to the downstream neuron when an AP arrives (2 inputs → it fires)
const DECAY = 0.94 // per-frame charge decay → isolated charge fades, so activity dissipates (calm)
const FANOUT = 4 // max synapses an firing neuron drives (bounds the cascade)
const MAX_AP = 220 // hard cap on action potentials in flight

interface AP {
  a: Vec3
  b: Vec3
  start: number
  dst: number // index of the downstream neuron this action potential charges on arrival
}

function Dendrites({ nodes, positions }: { nodes: GraphNode[]; positions: Map<string, Vec3> }): JSX.Element | null {
  // short spiky dendrite tufts radiating from each soma, in ONE line-segments geometry (one draw call).
  const geom = useMemo(() => {
    const pts: number[] = []
    const hash = (s: string, i: number) => {
      let h = 2166136261 ^ i
      for (let k = 0; k < s.length; k++) h = Math.imul(h ^ s.charCodeAt(k), 16777619)
      return ((h >>> 0) % 1000) / 1000
    }
    for (const n of nodes) {
      const p = positions.get(n.id)
      if (!p) continue
      const r = nodeRadius(n)
      const tufts = 5
      for (let i = 0; i < tufts; i++) {
        const t = (i + 0.5) / tufts
        const phi = Math.acos(1 - 2 * t)
        const theta = 2 * Math.PI * hash(n.id, i)
        const dx = Math.sin(phi) * Math.cos(theta)
        const dy = Math.cos(phi)
        const dz = Math.sin(phi) * Math.sin(theta)
        const len = r * (1.6 + hash(n.id, i + 9) * 1.2)
        pts.push(p[0] + dx * r, p[1] + dy * r, p[2] + dz * r)
        pts.push(p[0] + dx * len, p[1] + dy * len, p[2] + dz * len)
      }
    }
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array(pts), 3))
    return g
  }, [nodes, positions])
  if (!geom) return null
  return (
    <lineSegments geometry={geom}>
      <lineBasicMaterial color="#3f5468" transparent opacity={0.35} />
    </lineSegments>
  )
}

function Scene({ nodes, edges }: { nodes: GraphNode[]; edges: GraphEdge[] }): JSX.Element {
  const positions = useMemo(() => layout(nodes, edges), [nodes, edges])
  const index = useMemo(() => new Map(nodes.map((n, i) => [n.id, i])), [nodes])
  const nameToId = useMemo(() => {
    const m = new Map<string, string>()
    for (const n of nodes) m.set(n.name.toLowerCase(), n.id)
    return m
  }, [nodes])
  const adjacency = useMemo(() => {
    const a = new Map<string, string[]>()
    for (const e of edges) {
      ;(a.get(e.src_id) ?? a.set(e.src_id, []).get(e.src_id)!).push(e.dst_id)
      ;(a.get(e.dst_id) ?? a.set(e.dst_id, []).get(e.dst_id)!).push(e.src_id)
    }
    return a
  }, [edges])

  const activation = useStore((s) => s.activation)
  const selection = useStore((s) => s.selection)
  const select = useStore((s) => s.select)
  const presence = useStore((s) => s.presence?.presence)
  const lastSeq = useStore((s) => s.lastSeq)
  const pulsing = presence === 'thinking' || presence === 'executing' || presence === 'learning'

  const hubId = useMemo(() => nodes.find((n) => n.node_type === 'agent')?.id ?? null, [nodes])
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

  // ── the firing simulation (mutable refs; never re-renders React) ───────────────────────────────────
  const somaRefs = useRef<(Mesh | null)[]>([])
  const haloRefs = useRef<(Mesh | null)[]>([])
  const apRefs = useRef<(Mesh | null)[]>([])
  const charge = useRef<Float32Array>(new Float32Array(nodes.length))
  const lastFire = useRef<Float32Array>(new Float32Array(nodes.length).fill(-1e9))
  const aps = useRef<AP[]>([])
  const prevSeq = useRef(0)

  useEffect(() => {
    charge.current = new Float32Array(nodes.length)
    lastFire.current = new Float32Array(nodes.length).fill(-1e9)
    aps.current = []
  }, [nodes.length])

  const seedFire = (nodeId: string) => {
    const i = index.get(nodeId)
    if (i !== undefined) charge.current[i] = THRESHOLD // will fire next frame
  }
  // a real retrieval → the REAL nodes it used fire, seeding a cascade through their real synapses
  useEffect(() => {
    if (activation) for (const f of activation.graph) {
      const a = nameToId.get(f.src.toLowerCase())
      const b = nameToId.get(f.dst.toLowerCase())
      if (a) seedFire(a)
      if (b) seedFire(b)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activation])
  // every real event → a neuron fires (deterministic by seq); a burst fires ~N neurons
  useEffect(() => {
    const delta = Math.min(Math.max(lastSeq - prevSeq.current, 1), 6)
    prevSeq.current = lastSeq
    if (nodes.length) for (let k = 0; k < delta; k++) seedFire(nodes[(lastSeq + k * 7) % nodes.length].id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastSeq])

  useFrame(({ clock }) => {
    const now = performance.now()
    const ch = charge.current
    const lf = lastFire.current

    // 1) advance action potentials; when one arrives, it charges its downstream neuron (a synapse)
    const live: AP[] = []
    for (const ap of aps.current) {
      if (now - ap.start >= AP_MS) ch[ap.dst] = Math.min(THRESHOLD * 1.4, ch[ap.dst] + ARRIVE)
      else live.push(ap)
    }
    aps.current = live

    // 2) fire eligible neurons → spawn action potentials to neighbours
    for (let i = 0; i < nodes.length; i++) {
      if (ch[i] >= THRESHOLD && now - lf[i] > REFRACTORY) {
        lf[i] = now
        ch[i] = 0
        const neigh = adjacency.get(nodes[i].id) ?? []
        const pa = positions.get(nodes[i].id)
        for (let k = 0; k < neigh.length && k < FANOUT; k++) {
          if (aps.current.length >= MAX_AP) break
          const nb = neigh[(i + k) % neigh.length]
          const pb = positions.get(nb)
          const di = index.get(nb)
          if (pa && pb && di !== undefined) aps.current.push({ a: pa, b: pb, start: now, dst: di })
        }
      }
      ch[i] *= DECAY
    }

    // 3) render neurons: flash on fire, subtle glow while charging, calm when quiet
    for (let i = 0; i < nodes.length; i++) {
      const soma = somaRefs.current[i]
      if (!soma) continue
      const fire = Math.max(0, 1 - (now - lf[i]) / FLASH)
      const dim = neighborIds != null && !neighborIds.has(nodes[i].id) ? 0.16 : 1
      const breathe = pulsing && nodes[i].id === hubId ? (Math.sin(clock.elapsedTime * 3) * 0.5 + 0.5) * 0.4 : 0
      const sel = focusId === nodes[i].id ? 0.4 : 0
      const mat = soma.material as MeshStandardMaterial
      mat.emissiveIntensity = (0.32 + fire * 3.2 + ch[i] * 0.6 + breathe * 1.4 + sel) * dim
      const scale = (1 + fire * 0.55 + breathe + sel * 0.4) * (dim < 1 ? 0.7 : 1)
      soma.scale.setScalar(soma.scale.x + (scale - soma.scale.x) * 0.3)
      const halo = haloRefs.current[i]
      if (halo) {
        ;(halo.material as MeshStandardMaterial).opacity = (0.05 + fire * 0.5 + breathe * 0.2) * dim
        halo.scale.setScalar(soma.scale.x * (1 + fire * 0.6))
      }
    }

    // 4) render action potentials travelling the synapses
    for (let i = 0; i < MAX_AP; i++) {
      const m = apRefs.current[i]
      if (!m) continue
      const ap = aps.current[i]
      if (ap) {
        const t = (now - ap.start) / AP_MS
        const e = t * t * (3 - 2 * t)
        m.position.set(ap.a[0] + (ap.b[0] - ap.a[0]) * e, ap.a[1] + (ap.b[1] - ap.a[1]) * e, ap.a[2] + (ap.b[2] - ap.a[2]) * e)
        m.scale.setScalar(0.09 + Math.sin(t * Math.PI) * 0.16)
        m.visible = true
      } else m.visible = false
    }
  })

  return (
    <>
      <ambientLight intensity={0.5} />
      <pointLight position={[8, 10, 12]} intensity={0.65} />
      <pointLight position={[-10, -6, -8]} intensity={0.25} color="#4fe0cf" />
      <Dendrites nodes={nodes} positions={positions} />
      {edges.map((e) => {
        const a = positions.get(e.src_id)
        const b = positions.get(e.dst_id)
        if (!a || !b) return null
        return <Line key={e.id} points={[a, b]} color="#4d6376" lineWidth={1} transparent opacity={0.14} />
      })}
      {nodes.map((n, i) => {
        const p = positions.get(n.id)
        if (!p) return null
        const color = nodeColor(n.node_type)
        const r = nodeRadius(n)
        return (
          <group key={n.id} position={p}>
            <mesh ref={(el) => { haloRefs.current[i] = el }} scale={1}>
              <sphereGeometry args={[r * 2.1, 14, 14]} />
              <meshStandardMaterial color={color} emissive={color} emissiveIntensity={1} transparent opacity={0.06} depthWrite={false} />
            </mesh>
            <mesh
              ref={(el) => { somaRefs.current[i] = el }}
              onClick={(ev) => { ev.stopPropagation(); select({ kind: 'node', id: n.id, label: n.name }) }}
              onPointerOver={(ev) => { ev.stopPropagation(); document.body.style.cursor = 'pointer' }}
              onPointerOut={() => { document.body.style.cursor = 'auto' }}
            >
              <icosahedronGeometry args={[r, 1]} />
              <meshStandardMaterial color={color} emissive={color} emissiveIntensity={0.32} roughness={0.4} metalness={0.1} transparent />
            </mesh>
          </group>
        )
      })}
      {Array.from({ length: MAX_AP }, (_, i) => (
        <mesh key={`ap-${i}`} ref={(el) => { apRefs.current[i] = el }} visible={false}>
          <sphereGeometry args={[1, 8, 8]} />
          <meshBasicMaterial color="#b9fff2" toneMapped={false} transparent opacity={0.95} />
        </mesh>
      ))}
      {nodes.filter((n) => HUB_TYPES.has(n.node_type)).map((n) => {
        const p = positions.get(n.id)
        if (!p) return null
        return (
          <Html key={`l-${n.id}`} position={[p[0], p[1] + nodeRadius(n) + 0.3, p[2]]} center distanceFactor={12} style={{ pointerEvents: 'none' }}>
            <div className="whitespace-nowrap font-mono text-[10px] tracking-wide text-ink/80">{n.name}</div>
          </Html>
        )
      })}
      <OrbitControls enablePan={false} enableDamping dampingFactor={0.08} minDistance={6} maxDistance={34} autoRotate={!prefersReducedMotion()} autoRotateSpeed={0.22} />
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
        right={
          data ? (
            <span className="font-mono text-2xs text-faint">
              {data.nodes.length} neurons · {data.edges.length} synapses
              {collapsedCount > 0 && (
                <>
                  {' · '}
                  <button onClick={() => setToolsOpen(true)} className="text-accent-dim hover:text-accent">
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
        aria-label={data ? `Sali's neural memory: ${data.nodes.length} entities firing across ${data.edges.length} synapses. Details in the World panel and by selecting a neuron.` : 'loading'}
      >
        <div className="pointer-events-none absolute inset-0 z-10 vignette" />
        {isLoading && <Empty>waking the neurons…</Empty>}
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
