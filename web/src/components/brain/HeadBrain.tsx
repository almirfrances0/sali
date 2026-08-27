import { Html } from '@react-three/drei'
import { Canvas, useFrame } from '@react-three/fiber'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef } from 'react'
import { AdditiveBlending, BufferAttribute, BufferGeometry, Color, type Mesh, type MeshBasicMaterial, type Points, type PointsMaterial } from 'three'

import { api } from '../../lib/api'
import type { GraphNode } from '../../lib/types'
import { useStore } from '../../stores/store'
import { Empty } from '../ui/primitives'
import type { Vec3 } from './graph'
import { prefersReducedMotion } from './graph'
import { brainCore, filaments, headCloud, regionAnchor } from './headshape'
import { regionOf } from './regions'

// The cognitive regions that feed the brain (matching the reference), each with a colour + which real
// event types light it. Information flows from these anchors (left) into the brain (right).
const HEAD_REGIONS = [
  { id: 'perception', label: 'PERCEPTION', color: '#67e8f9', icon: 'M1 6 C4 1 10 1 13 6 C10 11 4 11 1 6 Z M7 4 a2 2 0 1 0 0 4 a2 2 0 0 0 0 -4', types: ['desktop.observed', 'twin.'] },
  { id: 'memory', label: 'MEMORY', color: '#b39dff', icon: 'M7 1 C3 1 1 4 2 7 C1 10 4 13 7 12 C10 13 13 10 12 7 C13 4 11 1 7 1', types: ['memory.'] },
  { id: 'knowledge', label: 'KNOWLEDGE', color: '#5eead4', icon: 'M7 2 L12 5 L12 9 L7 12 L2 9 L2 5 Z', types: ['twin.entity', 'memory.created'] },
  { id: 'experience', label: 'EXPERIENCE', color: '#6ee7b7', icon: 'M7 2 a5 5 0 1 0 0 10 a5 5 0 0 0 0 -10 M7 5 a2 2 0 1 0 0 4 a2 2 0 0 0 0 -4', types: ['learning.episode', 'conversation.'] },
  { id: 'learning', label: 'LEARNING', color: '#fbbf77', icon: 'M2 11 L5 7 L8 9 L12 3', types: ['learning.'] },
] as const
type HeadRegion = (typeof HEAD_REGIONS)[number]['id']

interface Stream { from: Vec3; to: Vec3; start: number; color: Color }

function HeadPoints({ cloud }: { cloud: ReturnType<typeof headCloud> }): JSX.Element {
  const ref = useRef<Points>(null)
  const geom = useMemo(() => {
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(cloud.positions, 3))
    g.setAttribute('color', new BufferAttribute(cloud.colors, 3))
    return g
  }, [cloud])
  const activity = useStore((s) => s.lastSeq)
  const pulse = useRef(0)
  useEffect(() => { pulse.current = 1 }, [activity])
  useFrame((_, dt) => {
    const m = ref.current?.material as PointsMaterial | undefined
    if (m) {
      pulse.current = Math.max(0, pulse.current - dt * 0.6)
      m.opacity = 0.62 + pulse.current * 0.25 + (prefersReducedMotion() ? 0 : Math.sin(performance.now() / 1400) * 0.05)
    }
  })
  return (
    <points ref={ref} geometry={geom}>
      <pointsMaterial size={0.05} vertexColors transparent opacity={0.9} sizeAttenuation depthWrite={false} blending={AdditiveBlending} />
    </points>
  )
}

function Filaments({ cloud }: { cloud: ReturnType<typeof headCloud> }): JSX.Element {
  const geom = useMemo(() => {
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(filaments(cloud, 2), 3))
    return g
  }, [cloud])
  return (
    <lineSegments geometry={geom}>
      <lineBasicMaterial color="#3f6a9c" transparent opacity={0.22} blending={AdditiveBlending} depthWrite={false} />
    </lineSegments>
  )
}

function Scene({ nodes }: { nodes: GraphNode[] }): JSX.Element {
  const cloud = useMemo(() => headCloud(), [])
  const core = useMemo(() => brainCore(), [])
  const activation = useStore((s) => s.activation)
  const lastSeq = useStore((s) => s.lastSeq)
  const events = useStore((s) => s.events)
  const presence = useStore((s) => s.presence?.presence)
  const select = useStore((s) => s.select)

  // real graph neurons live inside the brain cluster; they fire on events + retrieval, and are clickable
  const neurons = useMemo(() => {
    const h = (i: number, s: number) => { let x = 2166136261 ^ s; x = Math.imul(x ^ i, 16777619); return ((x >>> 0) % 1000) / 1000 }
    return nodes.map((n, i) => {
      const ang = h(i, 1) * Math.PI * 2
      const rr = Math.sqrt(h(i, 2))
      return {
        node: n,
        pos: [core[0] + Math.cos(ang) * 2.2 * rr, core[1] + Math.sin(ang) * 1.8 * rr, (h(i, 3) - 0.5) * 1.5] as Vec3,
        region: regionOf(n.node_type),
      }
    })
  }, [nodes, core])
  const nameToIdx = useMemo(() => new Map(nodes.map((n, i) => [n.name.toLowerCase(), i])), [nodes])

  const somaRefs = useRef<(Mesh | null)[]>([])
  const fireAt = useRef<Float32Array>(new Float32Array(nodes.length).fill(-1e9))
  const coreRef = useRef<Mesh>(null)
  const coreHalo = useRef<Mesh>(null)
  const anchorRefs = useRef<Record<HeadRegion, { mesh: Mesh | null; level: number }>>(
    Object.fromEntries(HEAD_REGIONS.map((r) => [r.id, { mesh: null, level: 0 }])) as any,
  )
  const streamRefs = useRef<(Mesh | null)[]>([])
  const streams = useRef<Stream[]>([])
  const coreLevel = useRef(0)
  const prevSeq = useRef(0)

  useEffect(() => { fireAt.current = new Float32Array(nodes.length).fill(-1e9) }, [nodes.length])

  const spawnStream = (rid: HeadRegion) => {
    const idx = HEAD_REGIONS.findIndex((r) => r.id === rid)
    const a = anchorRefs.current[rid]
    if (a) a.level = 1
    coreLevel.current = Math.min(1.4, coreLevel.current + 0.5)
    if (streams.current.length < 80) {
      streams.current.push({ from: regionAnchor(idx, HEAD_REGIONS.length), to: core, start: performance.now(), color: new Color(HEAD_REGIONS[idx].color) })
    }
  }
  const fireNeuron = (i: number) => { if (i >= 0 && i < nodes.length) fireAt.current[i] = performance.now() }

  // every real firehose event → the matching region streams info into the brain (§5)
  useEffect(() => {
    const fresh = events.filter((e) => e.seq > prevSeq.current)
    prevSeq.current = lastSeq
    for (const e of fresh) {
      for (const r of HEAD_REGIONS) if (r.types.some((t) => e.type === t || e.type.startsWith(t))) spawnStream(r.id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastSeq])

  // a real retrieval → memory streams in + the exact neurons it used fire
  const activeAt = activation?.at ?? 0
  useEffect(() => {
    if (!activation) return
    spawnStream('memory')
    spawnStream('knowledge')
    for (const f of activation.graph) {
      const a = nameToIdx.get(f.src.toLowerCase()); if (a !== undefined) fireNeuron(a)
      const b = nameToIdx.get(f.dst.toLowerCase()); if (b !== undefined) fireNeuron(b)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeAt])

  useFrame((state) => {
    const now = performance.now()
    // subtle parallax toward the pointer (a fixed profile view, gently alive)
    if (!prefersReducedMotion()) {
      const g = state.scene
      g.rotation.y += (state.pointer.x * 0.12 - g.rotation.y) * 0.04
      g.rotation.x += (-state.pointer.y * 0.06 - g.rotation.x) * 0.04
    }
    // presence keeps the brain core warm while Sali actually thinks/acts
    if (presence === 'thinking' || presence === 'executing' || presence === 'learning' || presence === 'observing')
      coreLevel.current = Math.max(coreLevel.current, 0.55)
    coreLevel.current *= 0.985

    // brain core pulse — a modest bright nucleus, never a giant disc
    if (coreRef.current) {
      const breathe = prefersReducedMotion() ? 0 : (Math.sin(now / 700) * 0.5 + 0.5) * 0.15
      const m = coreRef.current.material as MeshBasicMaterial
      m.opacity = 0.55 + coreLevel.current * 0.35
      coreRef.current.scale.setScalar(0.7 + coreLevel.current * 0.35 + breathe)
    }
    if (coreHalo.current) {
      ;(coreHalo.current.material as MeshBasicMaterial).opacity = 0.05 + coreLevel.current * 0.1
      coreHalo.current.scale.setScalar(1.1 + coreLevel.current * 0.9)
    }

    // region anchors decay + glow
    for (const r of HEAD_REGIONS) {
      const a = anchorRefs.current[r.id]
      a.level *= 0.94
      if (a.mesh) {
        ;(a.mesh.material as MeshBasicMaterial).opacity = 0.4 + a.level * 0.6
        a.mesh.scale.setScalar(0.28 + a.level * 0.3)
      }
    }

    // firing neurons
    for (let i = 0; i < nodes.length; i++) {
      const m = somaRefs.current[i]
      if (!m) continue
      const f = Math.max(0, 1 - (now - fireAt.current[i]) / 700)
      const mat = m.material as MeshBasicMaterial
      mat.opacity = 0.28 + f * 0.7
      m.scale.setScalar(0.035 + f * 0.09)
    }

    // streams flowing region → brain core
    streams.current = streams.current.filter((s) => now - s.start < 900)
    for (let i = 0; i < streamRefs.current.length; i++) {
      const m = streamRefs.current[i]
      if (!m) continue
      const s = streams.current[i]
      if (s) {
        const t = (now - s.start) / 900
        const e = t * t * (3 - 2 * t)
        // gentle arc: lift toward the middle
        const y = s.from[1] + (s.to[1] - s.from[1]) * e + Math.sin(t * Math.PI) * 1.2
        m.position.set(s.from[0] + (s.to[0] - s.from[0]) * e, y, s.from[2] + (s.to[2] - s.from[2]) * e)
        m.scale.setScalar(0.06 + Math.sin(t * Math.PI) * 0.12)
        ;(m.material as MeshBasicMaterial).color.copy(s.color)
        m.visible = true
      } else m.visible = false
    }
  })

  return (
    <>
      <fog attach="fog" args={['#05070a', 16, 46]} />
      <HeadPoints cloud={cloud} />
      <Filaments cloud={cloud} />

      {/* brain core */}
      <mesh ref={coreHalo} position={core}>
        <sphereGeometry args={[0.7, 16, 16]} />
        <meshBasicMaterial color="#ff59d8" transparent opacity={0.05} depthWrite={false} blending={AdditiveBlending} />
      </mesh>
      <mesh ref={coreRef} position={core}>
        <sphereGeometry args={[0.3, 20, 20]} />
        <meshBasicMaterial color="#ffa6ee" transparent opacity={0.7} toneMapped={false} blending={AdditiveBlending} />
      </mesh>

      {/* real graph neurons in the brain */}
      {neurons.map((n, i) => (
        <mesh
          key={n.node.id}
          position={n.pos}
          ref={(el) => { somaRefs.current[i] = el }}
          onClick={(ev) => { ev.stopPropagation(); select({ kind: 'node', id: n.node.id, label: n.node.name }) }}
          onPointerOver={(ev) => { ev.stopPropagation(); document.body.style.cursor = 'pointer' }}
          onPointerOut={() => { document.body.style.cursor = 'auto' }}
        >
          <sphereGeometry args={[1, 8, 8]} />
          <meshBasicMaterial color={HEAD_REGIONS.find((r) => r.id === n.region)?.color ?? '#8fb3ff'} transparent opacity={0.4} toneMapped={false} blending={AdditiveBlending} />
        </mesh>
      ))}

      {/* stream particles */}
      {Array.from({ length: 80 }, (_, i) => (
        <mesh key={`s-${i}`} ref={(el) => { streamRefs.current[i] = el }} visible={false}>
          <sphereGeometry args={[1, 6, 6]} />
          <meshBasicMaterial color="#9ff5e8" transparent opacity={0.95} toneMapped={false} blending={AdditiveBlending} />
        </mesh>
      ))}

      {/* region anchors on the left, with labels + icons */}
      {HEAD_REGIONS.map((r, i) => {
        const a = regionAnchor(i, HEAD_REGIONS.length)
        return (
          <group key={r.id} position={a}>
            <mesh ref={(el) => { if (anchorRefs.current[r.id]) anchorRefs.current[r.id].mesh = el }}>
              <sphereGeometry args={[0.28, 14, 14]} />
              <meshBasicMaterial color={r.color} transparent opacity={0.4} toneMapped={false} blending={AdditiveBlending} />
            </mesh>
            <Html position={[0.6, 0, 0]} center distanceFactor={13} style={{ pointerEvents: 'none' }}>
              <div className="flex items-center gap-1.5 whitespace-nowrap">
                <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke={r.color} strokeWidth="1.2" style={{ opacity: 0.9 }}>
                  <path d={r.icon} />
                </svg>
                <span className="font-mono text-[10px] uppercase tracking-[0.2em]" style={{ color: r.color }}>{r.label}</span>
              </div>
            </Html>
          </group>
        )
      })}
    </>
  )
}

export function HeadBrain(): JSX.Element {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['graph-snapshot'],
    queryFn: () => api.graphSnapshot(160),
    staleTime: 30_000,
  })
  return (
    <div className="relative h-full w-full">
      <div className="pointer-events-none absolute left-4 top-3 z-10">
        <span className="panel-label" style={{ letterSpacing: '0.22em' }}>Neural brain</span>
        <span className="ml-2 font-mono text-[10px] uppercase tracking-widest text-accent-dim">live activity</span>
      </div>
      <div className="pointer-events-none absolute right-4 top-3 z-10 text-right font-mono text-2xs text-faint">
        {data && (
          <>
            <div className="text-interesting">{data.nodes.length} neurons active</div>
            <div className="text-accent">{data.edges.length} connections</div>
          </>
        )}
      </div>
      {isLoading && <Empty>waking the neurons…</Empty>}
      {isError && <Empty>the brain is unreachable — is `sali serve` running?</Empty>}
      {data && (
        <Canvas camera={{ position: [0.4, 0, 15.5], fov: 46 }} dpr={[1, 2]} gl={{ antialias: true, alpha: true }}>
          <Scene nodes={data.nodes} />
        </Canvas>
      )}
    </div>
  )
}
