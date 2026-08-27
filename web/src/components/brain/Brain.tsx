import { Html, Line, OrbitControls } from '@react-three/drei'
import { Canvas, useFrame } from '@react-three/fiber'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef } from 'react'
import { AdditiveBlending, BufferAttribute, BufferGeometry, type Mesh, type MeshStandardMaterial, type Points } from 'three'

import { api } from '../../lib/api'
import type { GraphEdge, GraphNode } from '../../lib/types'
import { useStore } from '../../stores/store'
import { Empty, Panel } from '../ui/primitives'
import type { Vec3 } from './graph'
import { prefersReducedMotion } from './graph'
import {
  BRAIN, brainLayout, neuralDust, REGION_META, REGIONS, regionForPresence, regionOf, regionsForEvent,
  type Region,
} from './regions'

// firing dynamics (a bounded neural cascade — seeded by REAL events/retrievals, never a timer)
const THRESHOLD = 1.0
const REFRACTORY = 260
const FLASH = 360
const AP_MS = 520
const ARRIVE = 0.6
const DECAY = 0.94
const FANOUT = 4
const MAX_AP = 200

interface AP { a: Vec3; b: Vec3; start: number; dst: number }

// ── the brain silhouette: two translucent wireframe hemispheres + a soft inner membrane ─────────────
function Membrane(): JSX.Element {
  return (
    <group>
      {[1, -1].map((s) => (
        <mesh key={s} position={[s * 1.85, 0, 0]} scale={[BRAIN.A * 0.56, BRAIN.B, BRAIN.C]}>
          <icosahedronGeometry args={[1, 4]} />
          <meshBasicMaterial color="#6f8bb0" wireframe transparent opacity={0.05} depthWrite={false} />
        </mesh>
      ))}
      <mesh scale={[BRAIN.A, BRAIN.B, BRAIN.C]}>
        <sphereGeometry args={[1, 24, 24]} />
        <meshBasicMaterial color="#101826" transparent opacity={0.06} depthWrite={false} side={2} />
      </mesh>
    </group>
  )
}

// ── ambient neural dust (depth only, clearly not data) ──────────────────────────────────────────────
function Dust(): JSX.Element {
  const ref = useRef<Points>(null)
  const geom = useMemo(() => {
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(neuralDust(1500), 3))
    return g
  }, [])
  useFrame((_, dt) => {
    if (ref.current && !prefersReducedMotion()) ref.current.rotation.y += dt * 0.02
  })
  return (
    <points ref={ref} geometry={geom}>
      <pointsMaterial size={0.045} color="#5c7799" transparent opacity={0.5} sizeAttenuation depthWrite={false} blending={AdditiveBlending} />
    </points>
  )
}

// ── a cognitive region's glow — brightens when Sali actually uses that faculty ──────────────────────
function RegionGlow({ region, levelRef }: { region: Region; levelRef: { current: number } }): JSX.Element {
  const ref = useRef<Mesh>(null)
  const meta = REGION_META[region]
  useFrame(() => {
    const m = ref.current
    if (!m) return
    const lv = levelRef.current
    const mat = m.material as MeshStandardMaterial
    mat.opacity = 0.03 + lv * 0.16
    m.scale.setScalar(2.6 + lv * 1.4)
  })
  return (
    <mesh ref={ref} position={meta.center}>
      <sphereGeometry args={[1, 16, 16]} />
      <meshBasicMaterial color={meta.color} transparent opacity={0.03} depthWrite={false} blending={AdditiveBlending} />
    </mesh>
  )
}

// ── curved neural pathway (a synapse) — brightens while carrying real retrieval activity ────────────
function bow(a: Vec3, b: Vec3, k = 0.22): Vec3[] {
  const dir: Vec3 = [b[0] - a[0], b[1] - a[1], b[2] - a[2]]
  const px = dir[1], py = -dir[0] * 0.4, pz = dir[0] // a gentle out-of-line control offset
  const pl = Math.hypot(px, py, pz) || 1
  const off = k * Math.hypot(...dir)
  const c: Vec3 = [(a[0] + b[0]) / 2 + (px / pl) * off, (a[1] + b[1]) / 2 + (py / pl) * off, (a[2] + b[2]) / 2 + (pz / pl) * off]
  const pts: Vec3[] = []
  for (let i = 0; i <= 8; i++) {
    const t = i / 8
    const it = 1 - t
    pts.push([it * it * a[0] + 2 * it * t * c[0] + t * t * b[0], it * it * a[1] + 2 * it * t * c[1] + t * t * b[1], it * it * a[2] + 2 * it * t * c[2] + t * t * b[2]])
  }
  return pts
}

function Pathway({ a, b, activeRef }: { a: Vec3; b: Vec3; activeRef: { current: number } }): JSX.Element {
  const ref = useRef<any>(null)
  const pts = useMemo(() => bow(a, b), [a, b])
  useFrame(() => {
    const l = ref.current
    if (!l) return
    const k = activeRef.current
    l.material.opacity = 0.08 + k * 0.65
    l.material.color.setRGB(0.35 + k * 0.2, 0.5 + k * 0.4, 0.6 + k * 0.35)
  })
  return <Line ref={ref} points={pts} color="#4a6072" lineWidth={1} transparent opacity={0.08} />
}

function Scene({ nodes, edges }: { nodes: GraphNode[]; edges: GraphEdge[] }): JSX.Element {
  const positions = useMemo(() => brainLayout(nodes, edges), [nodes, edges])
  const index = useMemo(() => new Map(nodes.map((n, i) => [n.id, i])), [nodes])
  const nameToId = useMemo(() => new Map(nodes.map((n) => [n.name.toLowerCase(), n.id])), [nodes])
  const regionNodes = useMemo(() => {
    const m = new Map<Region, number[]>(REGIONS.map((r) => [r, []]))
    nodes.forEach((n, i) => m.get(regionOf(n.node_type))!.push(i))
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
  const events = useStore((s) => s.events)
  const lastSeq = useStore((s) => s.lastSeq)
  const presence = useStore((s) => s.presence?.presence)
  const selection = useStore((s) => s.selection)
  const select = useStore((s) => s.select)

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

  // firing + region-activation state (mutable refs; never re-renders React)
  const somaRefs = useRef<(Mesh | null)[]>([])
  const haloRefs = useRef<(Mesh | null)[]>([])
  const apRefs = useRef<(Mesh | null)[]>([])
  const charge = useRef(new Float32Array(nodes.length))
  const lastFire = useRef(new Float32Array(nodes.length).fill(-1e9))
  const aps = useRef<AP[]>([])
  const region = useRef<Record<Region, number>>(Object.fromEntries(REGIONS.map((r) => [r, 0])) as Record<Region, number>)
  const regionRefs = useRef<Record<Region, { current: number }>>(
    Object.fromEntries(REGIONS.map((r) => [r, { current: 0 }])) as Record<Region, { current: number }>,
  )
  const edgeActive = useRef(edges.map(() => ({ current: 0 })))
  const prevSeq = useRef(0)

  useEffect(() => {
    charge.current = new Float32Array(nodes.length)
    lastFire.current = new Float32Array(nodes.length).fill(-1e9)
    aps.current = []
  }, [nodes.length])
  useEffect(() => {
    edgeActive.current = edges.map(() => ({ current: 0 }))
  }, [edges])

  const fire = (i: number | undefined) => { if (i !== undefined) charge.current[i] = THRESHOLD }
  const activateRegion = (r: Region, amount: number) => {
    region.current[r] = Math.min(1, region.current[r] + amount)
    const pool = regionNodes.get(r) ?? []
    for (let k = 0; k < 3 && pool.length; k++) fire(pool[Math.floor((lastSeq + k * 13) % pool.length)])
  }

  // a real retrieval → the memory region lights + the exact neurons it used fire, seeding a cascade
  const activeAt = activation?.at ?? 0
  useEffect(() => {
    if (!activation) return
    activateRegion('memory', 0.9)
    activateRegion('knowledge', 0.5)
    for (const f of activation.graph) {
      fire(index.get(nameToId.get(f.src.toLowerCase()) ?? ''))
      fire(index.get(nameToId.get(f.dst.toLowerCase()) ?? ''))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeAt])

  // every real firehose event → light the region(s) it belongs to (§5)
  useEffect(() => {
    const fresh = events.filter((e) => e.seq > prevSeq.current)
    prevSeq.current = lastSeq
    for (const e of fresh) for (const r of regionsForEvent(e.type)) activateRegion(r, 0.55)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastSeq])

  const retrievedIds = useMemo(() => {
    const s = new Set<string>()
    if (activation) for (const f of activation.graph) {
      const a = nameToId.get(f.src.toLowerCase()); if (a) s.add(a)
      const b = nameToId.get(f.dst.toLowerCase()); if (b) s.add(b)
    }
    return s
  }, [activation, nameToId])

  useFrame(({ clock }) => {
    const now = performance.now()
    const ch = charge.current
    const lf = lastFire.current

    // presence keeps one region warm while Sali is in that state (real derived state)
    const pr = regionForPresence(presence)
    if (pr) region.current[pr] = Math.max(region.current[pr], 0.5)

    // action potentials: deliver charge on arrival
    const live: AP[] = []
    for (const ap of aps.current) {
      if (now - ap.start >= AP_MS) ch[ap.dst] = Math.min(THRESHOLD * 1.4, ch[ap.dst] + ARRIVE)
      else live.push(ap)
    }
    aps.current = live

    // fire eligible neurons → send APs to neighbours
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

    // region levels decay; publish to the glow refs
    for (const r of REGIONS) {
      region.current[r] *= 0.97
      regionRefs.current[r].current = region.current[r]
    }

    // neurons: flash on fire, faint charge glow, dim when a selection focuses elsewhere
    const retrievalK = Math.max(0, 1 - (Date.now() - activeAt) / 2600)
    for (let i = 0; i < nodes.length; i++) {
      const soma = somaRefs.current[i]
      if (!soma) continue
      const f = Math.max(0, 1 - (now - lf[i]) / FLASH)
      const dim = neighborIds != null && !neighborIds.has(nodes[i].id) ? 0.14 : 1
      const sel = focusId === nodes[i].id ? 0.5 : 0
      const mat = soma.material as MeshStandardMaterial
      mat.emissiveIntensity = (0.28 + f * 3.0 + ch[i] * 0.5 + sel) * dim
      const scale = (1 + f * 0.5 + sel * 0.4) * (dim < 1 ? 0.72 : 1)
      soma.scale.setScalar(soma.scale.x + (scale - soma.scale.x) * 0.3)
      const halo = haloRefs.current[i]
      if (halo) {
        ;(halo.material as MeshStandardMaterial).opacity = (0.05 + f * 0.45) * dim
        halo.scale.setScalar(soma.scale.x * (1 + f * 0.6))
      }
    }

    // pathways carrying retrieval activity
    for (let i = 0; i < edges.length; i++) {
      const e = edges[i]
      edgeActive.current[i].current = retrievedIds.has(e.src_id) && retrievedIds.has(e.dst_id) ? retrievalK : 0
    }

    // action-potential sprites travelling the synapses
    for (let i = 0; i < MAX_AP; i++) {
      const m = apRefs.current[i]
      if (!m) continue
      const ap = aps.current[i]
      if (ap) {
        const t = (now - ap.start) / AP_MS
        const e = t * t * (3 - 2 * t)
        m.position.set(ap.a[0] + (ap.b[0] - ap.a[0]) * e, ap.a[1] + (ap.b[1] - ap.a[1]) * e, ap.a[2] + (ap.b[2] - ap.a[2]) * e)
        m.scale.setScalar(0.08 + Math.sin(t * Math.PI) * 0.13)
        m.visible = true
      } else m.visible = false
    }
    void clock
  })

  return (
    <>
      <fog attach="fog" args={['#05070a', 15, 40]} />
      <ambientLight intensity={0.45} />
      <pointLight position={[8, 10, 12]} intensity={0.55} />
      <pointLight position={[-10, -6, -8]} intensity={0.22} color="#4fe0cf" />
      <Membrane />
      <Dust />
      {REGIONS.map((r) => <RegionGlow key={r} region={r} levelRef={regionRefs.current[r]} />)}
      {edges.map((e, i) => {
        const a = positions.get(e.src_id)
        const b = positions.get(e.dst_id)
        if (!a || !b) return null
        return <Pathway key={e.id} a={a} b={b} activeRef={edgeActive.current[i]} />
      })}
      {nodes.map((n, i) => {
        const p = positions.get(n.id)
        if (!p) return null
        const color = REGION_META[regionOf(n.node_type)].color
        const r = 0.13 + n.confidence * 0.14 + (n.node_type === 'agent' ? 0.22 : 0)
        return (
          <group key={n.id} position={p}>
            <mesh ref={(el) => { haloRefs.current[i] = el }}>
              <sphereGeometry args={[r * 2.2, 12, 12]} />
              <meshStandardMaterial color={color} emissive={color} emissiveIntensity={1} transparent opacity={0.05} depthWrite={false} blending={AdditiveBlending} />
            </mesh>
            <mesh
              ref={(el) => { somaRefs.current[i] = el }}
              onClick={(ev) => { ev.stopPropagation(); select({ kind: 'node', id: n.id, label: n.name }) }}
              onPointerOver={(ev) => { ev.stopPropagation(); document.body.style.cursor = 'pointer' }}
              onPointerOut={() => { document.body.style.cursor = 'auto' }}
            >
              <icosahedronGeometry args={[r, 1]} />
              <meshStandardMaterial color={color} emissive={color} emissiveIntensity={0.28} roughness={0.4} metalness={0.1} transparent />
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
      {REGIONS.map((r) => {
        const c = REGION_META[r].center
        return (
          <Html key={`rl-${r}`} position={[c[0], c[1] + 2.4, c[2]]} center distanceFactor={16} style={{ pointerEvents: 'none' }}>
            <div className="whitespace-nowrap font-mono text-[9px] uppercase tracking-[0.25em]" style={{ color: REGION_META[r].color, opacity: 0.55 }}>
              {REGION_META[r].label}
            </div>
          </Html>
        )
      })}
      <OrbitControls enablePan={false} enableDamping dampingFactor={0.08} minDistance={9} maxDistance={40} autoRotate={!prefersReducedMotion()} autoRotateSpeed={0.18} />
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
  const collapsedCount = Object.values(data?.collapsed ?? {}).reduce((a, b) => a + b, 0)

  return (
    <Panel className="!bg-transparent !border-0 !shadow-none">
      <div className="pointer-events-none absolute right-3 top-2 z-20 flex items-center gap-2 font-mono text-2xs text-faint">
        {data && <span>{data.nodes.length} neurons · {data.edges.length} synapses</span>}
        {collapsedCount > 0 && (
          <button onClick={() => setToolsOpen(true)} className="pointer-events-auto text-accent-dim hover:text-accent">
            {collapsedCount} tools ▸
          </button>
        )}
      </div>
      <div className="relative min-h-0 flex-1" role="img" aria-label={data ? `Sali's neural memory: ${data.nodes.length} entities across cognitive regions` : 'loading'}>
        {isLoading && <Empty>waking the neurons…</Empty>}
        {isError && <Empty>the brain is unreachable — is `sali serve` running?</Empty>}
        {data && (
          <Canvas camera={{ position: [0, 2, 20], fov: 50 }} dpr={[1, 2]} gl={{ antialias: true, alpha: true }}>
            <Scene nodes={data.nodes} edges={data.edges} />
          </Canvas>
        )}
      </div>
    </Panel>
  )
}
