import type { GraphEdge, GraphNode } from '../../lib/types'
import type { Vec3 } from './graph'

// Sali's brain is laid out as cognitive REGIONS in an anatomical silhouette (two hemispheres, an ovoid
// front-to-back). The real graph data determines the neurons inside each region; runtime EVENTS light the
// regions up. Regions with few graph nodes (perception/memory/learning) are event-driven — they glow when
// Sali actually perceives / remembers / learns, even if little permanent structure lives there.
export type Region = 'reasoning' | 'perception' | 'memory' | 'tools' | 'knowledge' | 'learning' | 'world'

export const REGIONS: Region[] = ['reasoning', 'perception', 'memory', 'tools', 'knowledge', 'learning', 'world']

export const REGION_META: Record<Region, { label: string; color: string; center: Vec3 }> = {
  // frontal lobe — reasoning / self / the model that thinks
  reasoning: { label: 'Reasoning', color: '#8aa2ff', center: [0, 0.7, 5.0] },
  // occipital, back — perception of the desktop / world
  perception: { label: 'Perception', color: '#67e8f9', center: [0, 0.2, -5.4] },
  // temporal / hippocampal, mid-low — memory
  memory: { label: 'Memory', color: '#c4a3ff', center: [0, -1.7, 0.6] },
  // motor strip, top-front — tools / action
  tools: { label: 'Tools', color: '#5eead4', center: [0, 2.7, 1.8] },
  // deep core — the knowledge graph
  knowledge: { label: 'Knowledge', color: '#93c5fd', center: [0, -0.1, 0.2] },
  // parietal, top-back — learning / consolidation
  learning: { label: 'Learning', color: '#f9a8d4', center: [0, 2.3, -2.6] },
  // brainstem / base — the machine world model
  world: { label: 'World', color: '#86efac', center: [0, -2.7, -0.6] },
}

const REGION_OF: Record<string, Region> = {
  agent: 'reasoning', person: 'reasoning', model: 'reasoning',
  machine: 'world', hardware: 'world', network: 'world', service: 'world', container: 'world',
  os_package: 'world', environment: 'world', host: 'world',
  capability: 'tools', ext_tool: 'tools',
  project: 'knowledge', location: 'knowledge', software: 'knowledge', entity: 'knowledge',
}

export function regionOf(nodeType: string): Region {
  return REGION_OF[nodeType] ?? 'knowledge'
}

function hash(s: string, salt = 0): number {
  let h = 2166136261 ^ salt
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619)
  return ((h >>> 0) % 100000) / 100000
}

// Brain envelope (ellipsoid): wider than tall, longer front-to-back.
export const BRAIN = { A: 6.8, B: 3.4, C: 7.6 }

// Place each neuron near its region centre, pushed into a hemisphere (a central fissure stays clear), and
// clamped inside the brain ellipsoid — so the whole cloud reads as an organic two-lobed brain.
export function brainLayout(nodes: GraphNode[], edges: GraphEdge[], iterations = 40): Map<string, Vec3> {
  const pos = new Map<string, Vec3>()
  for (const n of nodes) {
    const r = REGION_META[regionOf(n.node_type)].center
    const hemi = hash(n.id, 1) > 0.5 ? 1 : -1
    const rx = hash(n.id, 2)
    const ry = hash(n.id, 3) - 0.5
    const rz = hash(n.id, 4) - 0.5
    let x = hemi * (1.1 + rx * 3.2)
    let y = r[1] + ry * 2.6
    let z = r[2] + rz * 2.8
    // pull inside the ellipsoid
    const k = (x / BRAIN.A) ** 2 + (y / BRAIN.B) ** 2 + (z / BRAIN.C) ** 2
    if (k > 1) {
      const s = 1 / Math.sqrt(k)
      x *= s
      y *= s
      z *= s
    }
    pos.set(n.id, [x, y, z])
  }
  // a few light relaxation passes: connected neurons attract slightly, but region structure dominates
  const vel = new Map(nodes.map((n) => [n.id, [0, 0, 0] as Vec3]))
  const links = edges.filter((e) => pos.has(e.src_id) && pos.has(e.dst_id))
  for (let it = 0; it < iterations; it++) {
    for (const e of links) {
      const a = pos.get(e.src_id)!
      const b = pos.get(e.dst_id)!
      const dx = b[0] - a[0]
      const dy = b[1] - a[1]
      const dz = b[2] - a[2]
      const d = Math.sqrt(dx * dx + dy * dy + dz * dz) + 0.01
      const f = Math.min(0.04, (d - 3) * 0.01)
      const va = vel.get(e.src_id)!
      const vb = vel.get(e.dst_id)!
      va[0] += (dx / d) * f; va[1] += (dy / d) * f; va[2] += (dz / d) * f
      vb[0] -= (dx / d) * f; vb[1] -= (dy / d) * f; vb[2] -= (dz / d) * f
    }
    for (const n of nodes) {
      const p = pos.get(n.id)!
      const v = vel.get(n.id)!
      p[0] += v[0]; p[1] += v[1]; p[2] += v[2]
      v[0] *= 0.8; v[1] *= 0.8; v[2] *= 0.8
      // keep the fissure clear + stay in the ellipsoid
      if (Math.abs(p[0]) < 0.8) p[0] = p[0] < 0 ? -0.8 : 0.8
      const kk = (p[0] / BRAIN.A) ** 2 + (p[1] / BRAIN.B) ** 2 + (p[2] / BRAIN.C) ** 2
      if (kk > 1) {
        const s = 1 / Math.sqrt(kk)
        p[0] *= s; p[1] *= s; p[2] *= s
      }
    }
  }
  return pos
}

// Which cognitive region(s) a REAL runtime event lights up (§5). Only real event types; unknown → none.
export function regionsForEvent(type: string): Region[] {
  if (type.startsWith('memory.')) return ['memory']
  if (type.startsWith('tool.')) return ['tools']
  if (type.startsWith('learning.')) return ['learning']
  if (type.startsWith('twin.')) return ['world', 'perception']
  if (type === 'desktop.observed') return ['perception']
  if (type.startsWith('conversation.') || type === 'self.presence') return ['reasoning']
  if (type.startsWith('schedule.')) return ['reasoning']
  if (type.startsWith('sali.')) return ['reasoning', 'perception']
  return []
}

// The region that stays warm while Sali is in a given presence state (real derived state, not a timer).
export function regionForPresence(presence: string | undefined): Region | null {
  switch (presence) {
    case 'thinking':
      return 'reasoning'
    case 'executing':
      return 'tools'
    case 'learning':
      return 'learning'
    case 'observing':
      return 'perception'
    default:
      return null
  }
}

// A neural-dust particle cloud filling the brain volume (ambient depth, not data). Deterministic.
export function neuralDust(count: number): Float32Array {
  const arr = new Float32Array(count * 3)
  for (let i = 0; i < count; i++) {
    // rejection-free: sample a direction + radius, scale to ellipsoid
    const u = hash(`d${i}`, 5)
    const v = hash(`d${i}`, 6)
    const w = hash(`d${i}`, 7)
    const theta = 2 * Math.PI * u
    const phi = Math.acos(2 * v - 1)
    const rr = 0.35 + 0.65 * Math.cbrt(w)
    arr[i * 3] = rr * BRAIN.A * Math.sin(phi) * Math.cos(theta)
    arr[i * 3 + 1] = rr * BRAIN.B * Math.cos(phi)
    arr[i * 3 + 2] = rr * BRAIN.C * Math.sin(phi) * Math.sin(theta)
  }
  return arr
}
