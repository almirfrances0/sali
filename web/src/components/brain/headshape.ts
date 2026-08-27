import type { Vec3 } from './graph'

// A right-facing human head profile, in normalized space (x → right/front, y → up). The neural brain is
// rendered AS this silhouette: points along the outline are the skull filaments, points in the upper-front
// ellipse are the glowing brain, and the interior fill is neural tissue. Scaled + offset by the caller.
const OUTLINE: [number, number][] = [
  [-0.42, 0.86], [-0.16, 1.0], [0.2, 0.99], [0.48, 0.83], [0.61, 0.56], [0.585, 0.43],
  [0.69, 0.39], [0.9, 0.25], [0.72, 0.17], [0.8, 0.07], [0.77, -0.02], [0.69, -0.12],
  [0.65, -0.32], [0.5, -0.5], [0.33, -0.62], [0.29, -0.86], [0.23, -1.08],
  [-0.16, -1.08], [-0.2, -0.7], [-0.34, -0.34], [-0.49, 0.05], [-0.55, 0.5], [-0.42, 0.86],
]

function catmull(p0: number, p1: number, p2: number, p3: number, t: number): number {
  const t2 = t * t
  const t3 = t2 * t
  return 0.5 * (2 * p1 + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2 + (-p0 + 3 * p1 - 3 * p2 + p3) * t3)
}

// Smooth closed outline, sampled to `steps` points.
export function outlinePoints(steps = 320): [number, number][] {
  const pts: [number, number][] = []
  const n = OUTLINE.length
  for (let i = 0; i < n; i++) {
    const p0 = OUTLINE[(i - 1 + n) % n]
    const p1 = OUTLINE[i]
    const p2 = OUTLINE[(i + 1) % n]
    const p3 = OUTLINE[(i + 2) % n]
    const seg = Math.ceil(steps / n)
    for (let s = 0; s < seg; s++) {
      const t = s / seg
      pts.push([catmull(p0[0], p1[0], p2[0], p3[0], t), catmull(p0[1], p1[1], p2[1], p3[1], t)])
    }
  }
  return pts
}

function inside(poly: [number, number][], x: number, y: number): boolean {
  let c = false
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i]
    const [xj, yj] = poly[j]
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c
  }
  return c
}

function hash(i: number, salt: number): number {
  let h = 2166136261 ^ salt
  h = Math.imul(h ^ i, 16777619)
  h = Math.imul(h ^ (i >> 8), 16777619)
  return ((h >>> 0) % 100000) / 100000
}

// the brain sits in the upper-front of the head
const BRAIN_C: [number, number] = [0.06, 0.46]
const BRAIN_R: [number, number] = [0.44, 0.36]
export const SCALE = 5.4
export const OFFSET: Vec3 = [2.3, 0, 0]

function toWorld(x: number, y: number, z: number): Vec3 {
  return [x * SCALE + OFFSET[0], y * SCALE + OFFSET[1], z * SCALE + OFFSET[2]]
}

export function brainCore(): Vec3 {
  return toWorld(BRAIN_C[0], BRAIN_C[1], 0)
}

// Region anchors sit to the LEFT of the head; information flows from them into the brain.
export function regionAnchor(i: number, total: number): Vec3 {
  const y = 0.85 - (i / Math.max(1, total - 1)) * 1.7
  return [-6.4, y * SCALE * 0.7, 0]
}

export interface HeadCloud {
  positions: Float32Array
  colors: Float32Array
  count: number
}

function lerpColor(a: number[], b: number[], t: number): number[] {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]
}

// Generate the head point cloud: outline filaments + interior tissue + a dense glowing brain cluster.
export function headCloud(): HeadCloud {
  const outline = outlinePoints(340)
  const pos: number[] = []
  const col: number[] = []
  const violet = [0.55, 0.4, 1.0]
  const blue = [0.4, 0.6, 1.0]
  const cyan = [0.45, 0.9, 1.0]
  const magenta = [1.0, 0.35, 0.95]
  const green = [0.35, 0.9, 0.6]

  const push = (x: number, y: number, z: number, c: number[], jitter = 0) => {
    pos.push(x + (hash(pos.length, 1) - 0.5) * jitter, y + (hash(pos.length, 2) - 0.5) * jitter, z)
    col.push(c[0], c[1], c[2])
  }

  // 1) outline filaments (skull edge) — bright cyan/blue
  outline.forEach(([x, y], i) => {
    const z = (hash(i, 3) - 0.5) * 0.12
    push(x, y, z, lerpColor(cyan, blue, hash(i, 9)), 0.01)
  })

  // 2) interior neural tissue
  let i = 0
  let placed = 0
  while (placed < 1350 && i < 60000) {
    i++
    const x = -0.6 + hash(i, 4) * 1.6
    const y = -1.15 + hash(i, 5) * 2.25
    if (!inside(outline, x, y)) continue
    placed++
    const z = (hash(i, 6) - 0.5) * 0.5
    // colour by height: violet up top → blue mid → green toward the neck
    const t = (y + 1.15) / 2.3
    const base = t > 0.6 ? lerpColor(blue, violet, (t - 0.6) / 0.4) : lerpColor(green, blue, t / 0.6)
    push(x, y, z, base, 0)
  }

  // 3) the glowing brain cluster (dense, magenta→violet→cyan)
  for (let k = 0; k < 900; k++) {
    const ang = hash(k, 7) * Math.PI * 2
    const rr = Math.sqrt(hash(k, 8))
    const x = BRAIN_C[0] + Math.cos(ang) * BRAIN_R[0] * rr
    const y = BRAIN_C[1] + Math.sin(ang) * BRAIN_R[1] * rr
    if (!inside(outline, x, y)) continue
    const z = (hash(k, 11) - 0.5) * 0.6
    const c = rr < 0.35 ? magenta : lerpColor(magenta, violet, rr)
    push(x, y, z, c, 0.005)
  }

  const positions = new Float32Array(pos.length)
  const colors = new Float32Array(col.length)
  for (let j = 0; j < pos.length; j += 3) {
    const w = toWorld(pos[j], pos[j + 1], pos[j + 2])
    positions[j] = w[0]
    positions[j + 1] = w[1]
    positions[j + 2] = w[2]
  }
  colors.set(col)
  return { positions, colors, count: pos.length / 3 }
}

// nearest-neighbour filament segments (a capped web) over the cloud
export function filaments(cloud: HeadCloud, perPoint = 2): Float32Array {
  const { positions, count } = cloud
  const seg: number[] = []
  const step = Math.max(1, Math.floor(count / 900)) // subsample for O(n²/step) cost
  for (let a = 0; a < count; a += 1) {
    const ax = positions[a * 3]
    const ay = positions[a * 3 + 1]
    const az = positions[a * 3 + 2]
    const best: { d: number; b: number }[] = []
    for (let b = 0; b < count; b += step) {
      if (b === a) continue
      const dx = positions[b * 3] - ax
      const dy = positions[b * 3 + 1] - ay
      const dz = positions[b * 3 + 2] - az
      const d = dx * dx + dy * dy + dz * dz
      if (d < 0.55) best.push({ d, b })
    }
    best.sort((p, q) => p.d - q.d)
    for (let k = 0; k < perPoint && k < best.length; k++) {
      const b = best[k].b
      seg.push(ax, ay, az, positions[b * 3], positions[b * 3 + 1], positions[b * 3 + 2])
    }
  }
  return new Float32Array(seg)
}
