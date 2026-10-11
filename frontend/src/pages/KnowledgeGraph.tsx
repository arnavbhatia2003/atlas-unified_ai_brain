import { useEffect, useMemo, useRef, useState } from 'react'
import { Maximize2, Minus, Plus, SlidersHorizontal } from 'lucide-react'

import { GraphSphere, type GraphCanvasHandle } from '@/components/GraphSphere'
import { NodeInspector } from '@/components/NodeInspector'
import { PageHeader } from '@/components/PageHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Modal } from '@/components/ui/modal'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { NODE_TYPE_LABEL, nodeColor } from '@/lib/graphColors'
import { useAsync } from '@/lib/useAsync'
import type { GraphData, GraphLink, GraphNode, NodeType } from '@/lib/types'

function endId(end: string | GraphNode): string {
  return typeof end === 'object' ? end.id : end
}

// Tailwind `lg` breakpoint. The node inspector lives in the sticky sidebar on
// desktop and in a bottom-sheet modal on mobile. Because the modal renders
// through a portal, a `lg:hidden` wrapper can't suppress it — we gate its open
// state on viewport width instead.
function useIsDesktop() {
  const query = '(min-width: 1024px)'
  const [isDesktop, setIsDesktop] = useState(
    () => typeof window !== 'undefined' && window.matchMedia(query).matches,
  )
  useEffect(() => {
    const mql = window.matchMedia(query)
    const onChange = () => setIsDesktop(mql.matches)
    mql.addEventListener('change', onChange)
    onChange()
    return () => mql.removeEventListener('change', onChange)
  }, [])
  return isDesktop
}

export function KnowledgeGraph() {
  const isDesktop = useIsDesktop()
  const { data, loading, error } = useAsync<GraphData>((s) => api.graph(undefined, s))
  const canvasRef = useRef<GraphCanvasHandle>(null)
  const [selected, setSelected] = useState<GraphNode | null>(null)
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [disabled, setDisabled] = useState<Set<NodeType>>(new Set())
  // Hubs whose leaf-neighbor clusters have been expanded (key = `${hubId}::${type}`).
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const presentTypes = useMemo(() => {
    const t = new Set<NodeType>()
    data?.nodes.forEach((n) => t.add(n.type))
    return [...t]
  }, [data])

  const filtered: GraphData | null = useMemo(() => {
    if (!data) return null
    const nodes = data.nodes.filter((n) => !disabled.has(n.type))
    const keep = new Set(nodes.map((n) => n.id))
    const links = (data.links as GraphLink[]).filter(
      (l) => keep.has(endId(l.source)) && keep.has(endId(l.target)),
    )
    return { nodes, links, stats: { records: nodes.length, relationships: links.length } }
  }, [data, disabled])

  // Collapse dense leaf-neighbor groups into expandable cluster chips so a hub
  // with many records/identifiers stays readable (like the reference graphs).
  const display = useMemo(
    () => (filtered ? clusterGraph(filtered, expanded) : null),
    [filtered, expanded],
  )

  const toggle = (t: NodeType) =>
    setDisabled((prev) => {
      const next = new Set(prev)
      if (next.has(t)) {
        next.delete(t)
      } else {
        next.add(t)
      }
      return next
    })

  // Cluster chips expand on click; real nodes open the inspector.
  const handleSelect = (node: GraphNode | null) => {
    if (node && node.id.startsWith('cluster:')) {
      const key = node.id.slice('cluster:'.length)
      setExpanded((prev) => new Set(prev).add(key))
      return
    }
    setSelected(node)
  }

  // "Auto arrange": re-collapse every cluster and fit the whole graph in view.
  const autoArrange = () => {
    setExpanded(new Set())
    setTimeout(() => canvasRef.current?.fit(), 80)
  }

  return (
    <>
      <PageHeader
        eyebrow={
          data
            ? `Shared evidence model · ${data.stats.records} records · ${data.stats.relationships} relationships`
            : 'Shared evidence model'
        }
        title="Knowledge graph"
        description="Follow the evidence trail across equipment, records, permits, signals, and people. Everything traces to a source."
        action={
          <Button onClick={() => setFiltersOpen(true)}>
            <SlidersHorizontal />
            Filters
          </Button>
        }
      />

      <div className="grid gap-5 lg:grid-cols-[1fr_380px] lg:items-start">
        <Card className="relative overflow-hidden p-0">
          {data && data.nodes.length > 0 && (
            <div className="absolute right-3 top-3 z-10 flex flex-col gap-1.5">
              <Button
                variant="outline"
                size="icon"
                aria-label="Auto arrange and fit to view"
                title="Auto arrange · fit all nodes"
                onClick={autoArrange}
              >
                <Maximize2 />
              </Button>
              <Button
                variant="outline"
                size="icon"
                aria-label="Zoom in"
                onClick={() => canvasRef.current?.zoomIn()}
              >
                <Plus />
              </Button>
              <Button
                variant="outline"
                size="icon"
                aria-label="Zoom out"
                onClick={() => canvasRef.current?.zoomOut()}
              >
                <Minus />
              </Button>
            </div>
          )}
          <div className="absolute left-3 top-3 z-10">
            <Badge variant="verified">All sources verified</Badge>
          </div>

          {loading ? (
            <Skeleton className="m-4 h-[520px] rounded-xl" />
          ) : error ? (
            <EmptyCanvas message="Couldn't load the graph. Is the backend running?" />
          ) : display && display.nodes.length > 0 ? (
            <GraphSphere
              ref={canvasRef}
              data={display}
              selectedId={selected?.id}
              onSelect={handleSelect}
              height={560}
            />
          ) : (
            <EmptyCanvas message="No nodes to show. Ingest a source system, or adjust the filters." />
          )}
        </Card>

        {/* Inspector: desktop side panel — sticky, scrolls its own overflow so a
            long OKF bundle never pushes the page or gets cut off. */}
        <div className="hidden lg:block lg:sticky lg:top-4">
          {selected && filtered ? (
            <Card className="max-h-[calc(100vh-2rem)] overflow-y-auto py-5">
              <div className="px-5">
                <NodeInspector node={selected} data={filtered} />
              </div>
            </Card>
          ) : (
            <Card className="py-5">
              <div className="px-5 text-sm text-muted-foreground">
                Tap a node to pin it — the sphere stops spinning and its evidence,
                relations, and sources appear here.
              </div>
            </Card>
          )}
        </div>
      </div>

      {/* Inspector: mobile bottom sheet (scrollable). On desktop the detail
          lives in the sidebar above, so the modal stays closed there. */}
      <Modal open={!!selected && !isDesktop} onClose={() => setSelected(null)}>
        {selected && filtered && <NodeInspector node={selected} data={filtered} />}
      </Modal>

      {/* Filters */}
      <Modal
        open={filtersOpen}
        onClose={() => setFiltersOpen(false)}
        title="Graph filters"
        description="Choose which record types appear in the graph."
      >
        <div className="space-y-1">
          {presentTypes.map((t) => {
            const on = !disabled.has(t)
            return (
              <button
                key={t}
                onClick={() => toggle(t)}
                className="flex w-full items-center justify-between rounded-lg px-2 py-2.5 text-left text-sm transition-colors hover:bg-secondary"
              >
                <span className="flex items-center gap-2.5 font-medium">
                  <span
                    className="size-2.5"
                    style={{ background: nodeColor(t) }}
                    aria-hidden
                  />
                  {NODE_TYPE_LABEL[t]}
                </span>
                <span
                  className={
                    on
                      ? 'flex size-5 items-center justify-center rounded-md bg-primary text-primary-foreground'
                      : 'size-5 rounded-md border border-border'
                  }
                  aria-hidden
                >
                  {on && (
                    <svg viewBox="0 0 12 12" className="size-3" fill="none">
                      <path
                        d="M2.5 6.5l2.5 2.5 4.5-5"
                        stroke="currentColor"
                        strokeWidth="1.8"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  )}
                </span>
              </button>
            )
          })}
          {presentTypes.length === 0 && (
            <p className="text-sm text-muted-foreground">No record types in view.</p>
          )}
        </div>
      </Modal>
    </>
  )
}

function EmptyCanvas({ message }: { message: string }) {
  return (
    <div className="flex h-[560px] items-center justify-center p-6 text-center">
      <p className="max-w-sm text-sm text-muted-foreground">{message}</p>
    </div>
  )
}

// Degree above which a node's leaf neighbors get collapsed into cluster chips.
const HUB_THRESHOLD = 6
// Only collapse a same-type group of at least this many leaves.
const MIN_GROUP = 3

type ClusterNode = GraphNode & { __cluster?: true; __members?: string[]; __hub?: string }

/**
 * Collapse dense hubs: a node with many single-connection (leaf) neighbors has
 * those neighbors grouped, per type, into one expandable "+N" chip. Keeps the
 * graph legible at scale; multi-connection nodes (e.g. asset↔asset links) are
 * never collapsed. `expanded` holds `${hubId}::${type}` keys shown in full.
 */
function clusterGraph(g: GraphData, expanded: Set<string>): GraphData {
  const degree = new Map<string, number>()
  const adj = new Map<string, string[]>()
  const bump = (a: string, b: string) => {
    degree.set(a, (degree.get(a) ?? 0) + 1)
    if (!adj.has(a)) adj.set(a, [])
    adj.get(a)!.push(b)
  }
  for (const l of g.links) {
    const s = endId(l.source)
    const t = endId(l.target)
    bump(s, t)
    bump(t, s)
  }
  const nodeById = new Map(g.nodes.map((n) => [n.id, n]))
  const removed = new Set<string>()
  const clusterNodes: ClusterNode[] = []
  const clusterLinks: GraphLink[] = []

  for (const [hubId, deg] of degree) {
    if (deg <= HUB_THRESHOLD) continue
    const leaves = (adj.get(hubId) ?? []).filter((nid) => (degree.get(nid) ?? 0) === 1)
    const byType = new Map<NodeType, string[]>()
    for (const leaf of leaves) {
      const t = nodeById.get(leaf)?.type
      if (!t) continue
      if (!byType.has(t)) byType.set(t, [])
      byType.get(t)!.push(leaf)
    }
    for (const [type, members] of byType) {
      const key = `${hubId}::${type}`
      if (members.length < MIN_GROUP || expanded.has(key)) continue
      members.forEach((m) => removed.add(m))
      const clusterId = `cluster:${key}`
      clusterNodes.push({
        id: clusterId,
        type,
        label: `+${members.length} ${NODE_TYPE_LABEL[type] ?? type}`,
        __cluster: true,
        __members: members,
        __hub: hubId,
      })
      clusterLinks.push({
        source: hubId,
        target: clusterId,
        relation: 'GROUPED',
        layer: 'physical',
      })
    }
  }

  const nodes = g.nodes.filter((n) => !removed.has(n.id))
  const links = (g.links as GraphLink[]).filter(
    (l) => !removed.has(endId(l.source)) && !removed.has(endId(l.target)),
  )
  return {
    nodes: [...nodes, ...clusterNodes],
    links: [...links, ...clusterLinks],
    stats: {
      records: nodes.length + clusterNodes.length,
      relationships: links.length + clusterLinks.length,
    },
  }
}
