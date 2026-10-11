import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react'
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceRadial,
  forceSimulation,
} from 'd3-force-3d'

import { nodeColor } from '@/lib/graphColors'
import type { GraphData, GraphLink, GraphNode } from '@/lib/types'

export interface GraphCanvasHandle {
  zoomIn: () => void
  zoomOut: () => void
  fit: () => void
}

const ACCENT = '#ff2a2a'
const INK = '#eaeaea'
// Above this zoom, every node shows its full label chip. Below it we only draw
// dots + halos (an uncluttered overview), and labels appear just for the node
// under focus (hover / selected / its neighbours).
const LABEL_ZOOM = 1.5
const reducedMotion = () =>
  typeof window !== 'undefined' &&
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

function endId(end: string | GraphNode): string {
  return typeof end === 'object' ? end.id : end
}

interface P3 { x: number; y: number; z: number }

/**
 * Knowledge graph as a revolving sphere of chip-nodes on a 2D canvas. A 3D force
 * layout (with collision) spaces nodes apart on the ball; it auto-revolves about
 * its centre of mass while idle and projects with a depth cue — front nodes fully
 * opaque and larger, back nodes faded and smaller (the 3D illusion).
 *
 * To stay legible with many nodes it uses level-of-detail: zoomed out you see a
 * clean field of dots + edges with breathing room; hover/select a node (or zoom
 * in) to reveal the labelled chips. Hover lights up a node's edges + relations;
 * click focuses it, stops the spin, selects it.
 */
export const GraphSphere = forwardRef<
  GraphCanvasHandle,
  {
    data: GraphData
    selectedId?: string | null
    onSelect?: (node: GraphNode | null) => void
    height?: number
    className?: string
  }
>(function GraphSphere({ data, selectedId, onSelect, height = 560, className }, ref) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [size, setSize] = useState({ w: 600, h: height })

  const view = useRef({
    theta: 0.5,
    tilt: -0.32,
    zoom: 1,
    panX: 0,
    panY: 0,
    targetTheta: null as number | null,
    targetZoom: null as number | null,
    targetPanX: 0,
    targetPanY: 0,
    dragging: false,
    moved: false,
    lastX: 0,
    lastY: 0,
  })
  const pos = useRef<Map<string, P3>>(new Map())
  const rects = useRef<{ id: string; cx: number; cy: number; w: number; h: number; d: number }[]>([])
  const hoverRef = useRef<string | null>(null)
  const selRef = useRef<string | null>(selectedId ?? null)
  const dataRef = useRef(data)
  const onSelectRef = useRef(onSelect)
  dataRef.current = data
  onSelectRef.current = onSelect

  const adjacency = useMemo(() => {
    const m = new Map<string, Set<string>>()
    for (const l of data.links) {
      const s = endId(l.source), t = endId(l.target)
      if (!m.has(s)) m.set(s, new Set())
      if (!m.has(t)) m.set(t, new Set())
      m.get(s)!.add(t)
      m.get(t)!.add(s)
    }
    return m
  }, [data.links])

  // --- 3D force layout (run once per data change) --------------------------
  useEffect(() => {
    const nodes = data.nodes.map((n) => ({ id: n.id }))
    const links = data.links.map((l) => ({ source: endId(l.source), target: endId(l.target) }))
    const n = nodes.length || 1
    // Bigger ball + collision so nodes keep their distance instead of piling up.
    const R = 30 * Math.cbrt(n) + 52
    const sim = forceSimulation(nodes, 3)
      .force('charge', forceManyBody().strength(-95))
      .force('link', forceLink(links).id((d: { id: string }) => d.id).distance(60).strength(0.5))
      .force('center', forceCenter(0, 0, 0))
      .force('radial', forceRadial(R, 0, 0, 0).strength(0.32))
      .force('collide', forceCollide(17).strength(0.9))
      .stop()
    const ticks = Math.min(500, 160 + n * 6)
    for (let i = 0; i < ticks; i++) sim.tick()
    const m = new Map<string, P3>()
    for (const nd of nodes as unknown as (P3 & { id: string })[]) {
      m.set(nd.id, { x: nd.x || 0, y: nd.y || 0, z: nd.z || 0 })
    }
    pos.current = m
    // reset to a full zoomed-out overview whenever the graph changes
    const v = view.current
    v.zoom = 1
    v.panX = 0
    v.panY = 0
    v.targetZoom = null
    v.targetTheta = null
  }, [data])

  // --- resize --------------------------------------------------------------
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver((e) => setSize({ w: e[0].contentRect.width, h: height }))
    ro.observe(el)
    setSize({ w: el.clientWidth, h: height })
    return () => ro.disconnect()
  }, [height])

  useEffect(() => {
    selRef.current = selectedId ?? null
    const v = view.current
    if (selectedId) {
      const p = pos.current.get(selectedId)
      if (p) {
        v.targetTheta = -Math.atan2(p.x, p.z) // rotate node to the front
        v.targetZoom = Math.max(v.zoom, 1.7) // zoom in to focus the picked node
        v.targetPanX = 0
        v.targetPanY = 0
      }
    } else {
      v.targetTheta = null
      v.targetZoom = 1 // ease back out to the overview
    }
  }, [selectedId])

  useImperativeHandle(ref, () => ({
    zoomIn: () => { const v = view.current; v.targetZoom = null; v.zoom = Math.min(3.2, v.zoom * 1.25) },
    zoomOut: () => { const v = view.current; v.targetZoom = null; v.zoom = Math.max(0.5, v.zoom / 1.25) },
    fit: () => { const v = view.current; v.targetZoom = null; v.zoom = 1; v.panX = 0; v.panY = 0; v.targetPanX = 0; v.targetPanY = 0 },
  }))

  // --- render loop ---------------------------------------------------------
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')!
    let raf = 0
    const dpr = Math.min(2, window.devicePixelRatio || 1)

    const draw = () => {
      const { w, h } = size
      if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
        canvas.width = w * dpr
        canvas.height = h * dpr
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, w, h)

      const v = view.current
      const spinning =
        !reducedMotion() && !v.dragging && !selRef.current && !hoverRef.current
      if (spinning) v.theta += 0.0016
      // ease toward focus target (on select / deselect)
      if (v.targetTheta != null) {
        v.theta += (v.targetTheta - v.theta) * 0.12
        v.panX += (v.targetPanX - v.panX) * 0.12
        v.panY += (v.targetPanY - v.panY) * 0.12
        if (Math.abs(v.targetTheta - v.theta) < 0.001) v.targetTheta = null
      }
      if (v.targetZoom != null) {
        v.zoom += (v.targetZoom - v.zoom) * 0.12
        if (Math.abs(v.targetZoom - v.zoom) < 0.004) { v.zoom = v.targetZoom; v.targetZoom = null }
      }

      const cosT = Math.cos(v.theta), sinT = Math.sin(v.theta)
      const cosP = Math.cos(v.tilt), sinP = Math.sin(v.tilt)
      const nodes = dataRef.current.nodes
      const project = (p: P3) => {
        const rx = p.x * cosT + p.z * sinT
        const rz = -p.x * sinT + p.z * cosT
        const ry = p.y
        const ty = ry * cosP - rz * sinP
        const tz = ry * sinP + rz * cosP
        return { x: rx, y: ty, z: tz }
      }

      // auto-fit scale from layout radius (rotation preserves radius). Extra
      // margin keeps the whole ball comfortably inside the frame at zoom 1.
      let maxR = 1
      for (const nd of nodes) {
        const p = pos.current.get(nd.id)
        if (!p) continue
        const r = Math.hypot(p.x, p.y, p.z)
        if (r > maxR) maxR = r
      }
      const base = (Math.min(w, h) / 2 - 64) / maxR
      const scale = base * v.zoom
      const cx = w / 2 + v.panX
      const cy = h / 2 + v.panY

      const proj = new Map<string, { sx: number; sy: number; d: number }>()
      let minZ = Infinity, maxZ = -Infinity
      for (const nd of nodes) {
        const p = pos.current.get(nd.id)
        if (!p) continue
        const q = project(p)
        proj.set(nd.id, { sx: cx + q.x * scale, sy: cy + q.y * scale, d: q.z })
        if (q.z < minZ) minZ = q.z
        if (q.z > maxZ) maxZ = q.z
      }
      const depthOf = (d: number) => (maxZ === minZ ? 1 : (d - minZ) / (maxZ - minZ))

      const activeId = hoverRef.current ?? selRef.current
      const activeSet = activeId ? adjacency.get(activeId) : null
      const zoomedLabels = v.zoom >= LABEL_ZOOM

      // --- edges (behind nodes) ---
      ctx.lineCap = 'round'
      for (const l of dataRef.current.links as GraphLink[]) {
        const s = proj.get(endId(l.source)), t = proj.get(endId(l.target))
        if (!s || !t) continue
        const active =
          activeId != null &&
          (endId(l.source) === activeId || endId(l.target) === activeId)
        const dep = depthOf((s.d + t.d) / 2)
        const op = (active ? 0.92 : activeId ? 0.1 : 0.28) * (0.42 + 0.58 * dep)
        ctx.beginPath()
        ctx.moveTo(s.sx, s.sy)
        ctx.lineTo(t.sx, t.sy)
        ctx.strokeStyle = active ? `rgba(255,42,42,${op})` : `rgba(150,150,144,${op})`
        ctx.lineWidth = active ? 2.5 : 1.1
        ctx.stroke()
        // relation label at midpoint — only for the focused node's edges
        const rel = String(l.relation || '').toLowerCase().replace(/_/g, ' ')
        if (rel && active) {
          const mx = (s.sx + t.sx) / 2, my = (s.sy + t.sy) / 2
          const fs = 11
          ctx.font = `600 ${fs}px "JetBrains Mono", ui-monospace, monospace`
          ctx.textAlign = 'center'
          ctx.textBaseline = 'middle'
          // white stroke halo (paint-order stroke) like the reference line-labels
          ctx.lineJoin = 'round'
          ctx.lineWidth = 5
          ctx.strokeStyle = 'rgba(10,10,10,0.92)'
          ctx.strokeText(rel, mx, my)
          ctx.fillStyle = ACCENT
          ctx.fillText(rel, mx, my)
        }
      }

      // --- nodes (back to front) ---
      const order = nodes
        .map((nd) => ({ nd, p: proj.get(nd.id) }))
        .filter((o) => o.p) as { nd: GraphNode; p: { sx: number; sy: number; d: number } }[]
      order.sort((a, b) => a.p.d - b.p.d)

      rects.current = []
      for (const { nd, p } of order) {
        const dep = depthOf(p.d)
        const selected = nd.id === selRef.current
        const active = nd.id === activeId || (activeSet?.has(nd.id) ?? false)
        const isCluster = nd.id.startsWith('cluster:')
        const col = nodeColor(nd.type)
        const dScale = 0.72 + 0.5 * dep
        let alpha = 0.34 + 0.66 * dep
        if (activeId && !active) alpha *= 0.45

        // Level of detail: full labelled chip when zoomed in, or when this node
        // is the focus / a neighbour of the focus. Otherwise just a dot + halo.
        const showChip = zoomedLabels || active

        if (!showChip) {
          const dotR = (isCluster ? 6.5 : 5.5) * dScale
          const halo = dotR + 4 * dScale
          ctx.globalAlpha = alpha
          ctx.beginPath()
          ctx.arc(p.sx, p.sy, halo, 0, 2 * Math.PI)
          ctx.fillStyle = withAlpha(col, 0.22)
          ctx.fill()
          ctx.beginPath()
          ctx.arc(p.sx, p.sy, dotR, 0, 2 * Math.PI)
          ctx.fillStyle = isCluster ? '#6e6e68' : col
          ctx.fill()
          if (selected) {
            ctx.strokeStyle = ACCENT
            ctx.lineWidth = 2
            ctx.beginPath()
            ctx.arc(p.sx, p.sy, halo + 2, 0, 2 * Math.PI)
            ctx.stroke()
          }
          ctx.globalAlpha = 1
          const hit = Math.max(30, (halo + 4) * 2)
          rects.current.push({ id: nd.id, cx: p.sx, cy: p.sy, w: hit, h: hit, d: p.d })
          continue
        }

        // --- full chip ---
        const fs = Math.max(10, 12 * dScale)
        ctx.font = `${selected || active ? 700 : 500} ${fs}px "JetBrains Mono", ui-monospace, monospace`
        const label = nd.label || nd.id
        const tw = ctx.measureText(label).width
        const dotR = fs * 0.32
        const padX = fs * 0.72
        const gap = fs * 0.5
        const wch = dotR * 2 + gap + tw + padX * 2
        const hch = fs + fs * 1.05
        const x = p.sx - wch / 2
        const y = p.sy - hch / 2
        const r = 0

        ctx.globalAlpha = alpha
        roundRect(ctx, x, y, wch, hch, r)
        ctx.fillStyle = isCluster ? '#141414' : '#111111'
        ctx.fill()
        ctx.shadowColor = 'transparent'
        ctx.shadowBlur = 0
        ctx.shadowOffsetY = 0
        ctx.lineWidth = selected ? 2 : 1
        if (selected || active) ctx.strokeStyle = ACCENT
        else ctx.strokeStyle = `rgba(234,234,234,${(isCluster ? 0.4 : 0.22) * alpha + 0.05})`
        if (isCluster) ctx.setLineDash([3, 2])
        roundRect(ctx, x, y, wch, hch, r)
        ctx.stroke()
        ctx.setLineDash([])

        // dot + soft halo ring
        const dcx = x + padX + dotR, dcy = p.sy
        ctx.beginPath()
        ctx.arc(dcx, dcy, dotR + 3.5, 0, 2 * Math.PI)
        ctx.fillStyle = withAlpha(col, 0.22)
        ctx.fill()
        ctx.beginPath()
        ctx.arc(dcx, dcy, dotR, 0, 2 * Math.PI)
        ctx.fillStyle = isCluster ? '#6e6e68' : col
        ctx.fill()

        // label
        ctx.textAlign = 'left'
        ctx.textBaseline = 'middle'
        ctx.fillStyle = isCluster ? `rgba(234,234,234,${0.62 * alpha})` : withAlpha(INK, alpha)
        ctx.fillText(label, dcx + dotR + gap, dcy)
        ctx.globalAlpha = 1

        rects.current.push({ id: nd.id, cx: p.sx, cy: p.sy, w: wch, h: hch, d: p.d })
      }

      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [size, adjacency])

  // --- pointer interaction -------------------------------------------------
  const hitTest = (mx: number, my: number): string | null => {
    // front-most (largest depth) chip/dot under the cursor
    let best: string | null = null
    let bestD = -Infinity
    for (const r of rects.current) {
      if (
        mx >= r.cx - r.w / 2 && mx <= r.cx + r.w / 2 &&
        my >= r.cy - r.h / 2 && my <= r.cy + r.h / 2 && r.d > bestD
      ) {
        best = r.id
        bestD = r.d
      }
    }
    return best
  }

  const onDown = (e: React.PointerEvent) => {
    const v = view.current
    v.dragging = true
    v.moved = false
    v.lastX = e.clientX
    v.lastY = e.clientY
    try {
      ;(e.target as HTMLElement).setPointerCapture?.(e.pointerId)
    } catch {
      /* ignore invalid/absent pointer id */
    }
  }
  const onMove = (e: React.PointerEvent) => {
    const rect = canvasRef.current!.getBoundingClientRect()
    const mx = e.clientX - rect.left, my = e.clientY - rect.top
    const v = view.current
    if (v.dragging) {
      const dx = e.clientX - v.lastX, dy = e.clientY - v.lastY
      if (Math.abs(dx) + Math.abs(dy) > 3) v.moved = true
      v.theta -= dx * 0.006
      v.tilt = Math.max(-1.2, Math.min(1.2, v.tilt + dy * 0.006))
      v.lastX = e.clientX
      v.lastY = e.clientY
      v.targetTheta = null
    } else {
      const id = hitTest(mx, my)
      if (id !== hoverRef.current) {
        hoverRef.current = id
        if (canvasRef.current) canvasRef.current.style.cursor = id ? 'pointer' : 'grab'
      }
    }
  }
  const onUp = (e: React.PointerEvent) => {
    const v = view.current
    const rect = canvasRef.current!.getBoundingClientRect()
    const mx = e.clientX - rect.left, my = e.clientY - rect.top
    if (!v.moved) {
      const id = hitTest(mx, my)
      const node = id ? dataRef.current.nodes.find((n) => n.id === id) ?? null : null
      onSelectRef.current?.(node)
    }
    v.dragging = false
  }
  const onWheel = (e: React.WheelEvent) => {
    const v = view.current
    v.targetZoom = null
    v.zoom = Math.max(0.5, Math.min(3.2, v.zoom * (1 - e.deltaY * 0.0012)))
  }

  return (
    <div ref={wrapRef} className={className} style={{ height }}>
      <canvas
        ref={canvasRef}
        style={{ width: '100%', height, display: 'block', cursor: 'grab', touchAction: 'none' }}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerLeave={() => { hoverRef.current = null; view.current.dragging = false }}
        onWheel={onWheel}
      />
    </div>
  )
})

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath()
  ctx.roundRect(x, y, w, h, r)
}

function withAlpha(hex: string, a: number): string {
  const h = hex.replace('#', '')
  const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`
}
