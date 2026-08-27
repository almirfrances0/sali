import type { GraphEdge, GraphNode } from '../../lib/types'

export type Vec3 = [number, number, number]

// Node colour by entity type — a restrained, cool scientific palette (not rainbow). The agent (Sali) and
// the person (Almir) are the warm anchors; everything else is cool.
const TYPE_COLOR: Record<string, string> = {
  agent: '#4fe0cf',
  person: '#ffd27d',
  machine: '#8ab4ff',
  model: '#b39dff',
  service: '#6ee7b7',
  container: '#7dd3fc',
  project: '#f0abfc',
  software: '#93c5fd',
  capability: '#5eead4',
  hardware: '#fca5a5',
  network: '#86efac',
  environment: '#a5b4fc',
  location: '#cbd5e1',
  host: '#8ab4ff',
  entity: '#dbe4f0',
  ext_tool: '#5b6b80',
}

export function nodeColor(t: string): string {
  return TYPE_COLOR[t] ?? '#9fb2c8'
}

// Radius from confidence + a bump for the anchor types, so hubs read as larger neurons.
export function nodeRadius(n: GraphNode): number {
  const base = 0.16 + n.confidence * 0.16
  const bump = n.node_type === 'agent' ? 0.34 : n.node_type === 'machine' || n.node_type === 'person' ? 0.2 : 0
  return base + bump
}

// A cheap deterministic hash (no Math.random — layout must be stable across renders/reloads).
function hash(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return (h >>> 0) / 4294967295
}

// A compact 3D force-directed layout, computed ONCE per snapshot (memoized by the caller). Deterministic:
// positions seed from a hashed Fibonacci sphere, then repulsion + edge springs + gentle centering relax
// it into an organic neural shape. O(n²) repulsion is fine for the bounded core (≤ ~250 nodes).
export function layout(nodes: GraphNode[], edges: GraphEdge[], iterations = 140): Map<string, Vec3> {
  const pos = new Map<string, Vec3>()
  const vel = new Map<string, Vec3>()
  const n = nodes.length || 1
  nodes.forEach((node, i) => {
    const t = (i + 0.5) / n
    const phi = Math.acos(1 - 2 * t)
    const theta = 2 * Math.PI * hash(node.id) + i * 2.399963
    const r = 7
    pos.set(node.id, [r * Math.sin(phi) * Math.cos(theta), r * Math.cos(phi), r * Math.sin(phi) * Math.sin(theta)])
    vel.set(node.id, [0, 0, 0])
  })
  const ids = [...pos.keys()]
  const links = edges.filter((e) => pos.has(e.src_id) && pos.has(e.dst_id))

  for (let it = 0; it < iterations; it++) {
    for (let a = 0; a < ids.length; a++) {
      const pa = pos.get(ids[a])!
      let fx = 0
      let fy = 0
      let fz = 0
      for (let b = 0; b < ids.length; b++) {
        if (a === b) continue
        const pb = pos.get(ids[b])!
        const dx = pa[0] - pb[0]
        const dy = pa[1] - pb[1]
        const dz = pa[2] - pb[2]
        const d2 = dx * dx + dy * dy + dz * dz + 0.05
        const d = Math.sqrt(d2)
        const f = 2.6 / d2
        fx += (dx / d) * f
        fy += (dy / d) * f
        fz += (dz / d) * f
      }
      fx -= pa[0] * 0.018
      fy -= pa[1] * 0.018
      fz -= pa[2] * 0.018
      const v = vel.get(ids[a])!
      v[0] = (v[0] + fx) * 0.86
      v[1] = (v[1] + fy) * 0.86
      v[2] = (v[2] + fz) * 0.86
    }
    for (const e of links) {
      const pa = pos.get(e.src_id)!
      const pb = pos.get(e.dst_id)!
      const dx = pb[0] - pa[0]
      const dy = pb[1] - pa[1]
      const dz = pb[2] - pa[2]
      const d = Math.sqrt(dx * dx + dy * dy + dz * dz) + 0.01
      const f = (d - 3.1) * 0.055
      const ux = dx / d
      const uy = dy / d
      const uz = dz / d
      const va = vel.get(e.src_id)!
      const vb = vel.get(e.dst_id)!
      va[0] += ux * f
      va[1] += uy * f
      va[2] += uz * f
      vb[0] -= ux * f
      vb[1] -= uy * f
      vb[2] -= uz * f
    }
    for (const id of ids) {
      const p = pos.get(id)!
      const v = vel.get(id)!
      p[0] += v[0] * 0.1
      p[1] += v[1] * 0.1
      p[2] += v[2] * 0.1
    }
  }
  return pos
}

export function prefersReducedMotion(): boolean {
  return typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches
}
