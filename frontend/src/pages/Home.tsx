import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowRight,
  FileText,
  Radio,
  ShieldCheck,
  Upload,
} from 'lucide-react'

import { GraphSphere } from '@/components/GraphSphere'
import { NodeInspector } from '@/components/NodeInspector'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Modal } from '@/components/ui/modal'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { useAsync } from '@/lib/useAsync'
import type { GraphNode, Overview, RecentEvidence } from '@/lib/types'

function greeting(): string {
  const h = new Date().getHours()
  if (h < 12) return 'Good morning.'
  if (h < 18) return 'Good afternoon.'
  return 'Good evening.'
}

export function Home() {
  const overview = useAsync<Overview>((s) => api.overview(s))
  const graph = useAsync((s) => api.graph(undefined, s))
  const [selected, setSelected] = useState<GraphNode | null>(null)

  const o = overview.data
  const hasData = (o?.source_records ?? 0) > 0

  return (
    <>
      <div className="mb-8">
        <h1 className="font-display text-3xl uppercase tracking-tight">
          <span className="mark-accent">{greeting()}</span>
        </h1>
        <p className="mt-2 max-w-xl text-sm leading-relaxed text-muted-foreground">
          {hasData
            ? 'A short operational summary — not a telemetry wall. Everything traces back to a source.'
            : 'No evidence has been ingested yet. Start by adding a source system.'}
        </p>
      </div>

      {/* Priorities */}
      <section className="mb-10">
        <div className="grid gap-4 sm:grid-cols-3">
          <MetricCard
            loading={overview.loading}
            value={o?.review_open ?? 0}
            label="items awaiting review"
          />
          <MetricCard
            loading={overview.loading}
            value={o?.unified_assets ?? 0}
            label="unified assets resolved"
          />
          <MetricCard
            loading={overview.loading}
            value={o?.source_records ?? 0}
            label="source records indexed"
          />
        </div>
      </section>

      {/* Graph preview + start work */}
      <section className="mb-10 grid gap-6 lg:grid-cols-[1.6fr_1fr]">
        <div>
          <div className="mb-3 flex items-end justify-between">
            <div>
              <h2 className="font-display text-sm uppercase">Knowledge graph preview</h2>
              <p className="text-sm text-muted-foreground">
                Select a node to inspect the shared evidence record.
              </p>
            </div>
            <Button variant="outline" size="sm" asChild>
              <Link to="/graph">
                Open graph
                <ArrowRight />
              </Link>
            </Button>
          </div>
          <Card className="overflow-hidden p-0">
            {graph.data && graph.data.nodes.length > 0 ? (
              <div className="p-2">
                <div className="px-3 pt-2">
                  <Badge variant="verified">
                    {graph.data.stats.records} linked records
                  </Badge>
                </div>
                <GraphSphere
                  data={graph.data}
                  selectedId={selected?.id}
                  onSelect={setSelected}
                  height={340}
                />
                <p className="px-3 pb-2 text-xs text-muted-foreground">
                  Auto-revolves at rest · drag to spin · tap a node for its evidence.
                </p>
              </div>
            ) : (
              <div className="flex h-[340px] flex-col items-center justify-center gap-2 text-center">
                {graph.loading ? (
                  <Skeleton className="h-40 w-11/12" />
                ) : (
                  <>
                    <p className="text-sm font-medium">No graph yet</p>
                    <p className="max-w-xs text-sm text-muted-foreground">
                      Ingest a source system to build the knowledge graph.
                    </p>
                    <Button size="sm" variant="outline" asChild className="mt-1">
                      <Link to="/connectors">
                        <Upload />
                        Connect a source
                      </Link>
                    </Button>
                  </>
                )}
              </div>
            )}
          </Card>
        </div>

        <div>
          <h2 className="mb-3 font-display text-sm uppercase">Start an analysis</h2>
          <div className="flex flex-col gap-3">
            <WorkflowLink
              to="/workflows/rca"
              icon={<Radio className="size-4" />}
              title="Root-cause analysis"
              desc="Score hypotheses against evidence, preserve open questions."
            />
            <WorkflowLink
              to="/workflows/compliance"
              icon={<ShieldCheck className="size-4" />}
              title="Compliance review"
              desc="Trace each clause back to a source record."
            />
            <WorkflowLink
              to="/connectors"
              icon={<Upload className="size-4" />}
              title="Connect a source"
              desc="Register a source; syncs pull only new records."
            />
          </div>
        </div>
      </section>

      {/* Recent evidence */}
      <section>
        <h2 className="font-display text-sm uppercase">Recent evidence</h2>
        <p className="mb-4 text-sm text-muted-foreground">
          The latest source records added to the index.
        </p>
        <Card className="gap-0 divide-y divide-border/60 p-0">
          {overview.loading ? (
            <div className="space-y-3 p-5">
              <Skeleton className="h-5 w-2/3" />
              <Skeleton className="h-5 w-1/2" />
            </div>
          ) : o && o.recent_evidence.length > 0 ? (
            o.recent_evidence.map((e) => <EvidenceRow key={e.citation} e={e} />)
          ) : (
            <p className="p-5 text-sm text-muted-foreground">
              No evidence indexed yet.
            </p>
          )}
        </Card>
      </section>

      <Modal open={!!selected} onClose={() => setSelected(null)}>
        {selected && graph.data && (
          <NodeInspector node={selected} data={graph.data} />
        )}
      </Modal>
    </>
  )
}

function MetricCard({
  value,
  label,
  loading,
}: {
  value: number
  label: string
  loading: boolean
}) {
  return (
    <Card className="items-center gap-2 py-6 text-center">
      {loading ? (
        <Skeleton className="h-9 w-12" />
      ) : (
        <span className="font-display text-3xl tracking-tight tabular-nums">
          {value}
        </span>
      )}
      <p className="text-sm text-muted-foreground">{label}</p>
    </Card>
  )
}

function WorkflowLink({
  to,
  icon,
  title,
  desc,
}: {
  to: string
  icon: React.ReactNode
  title: string
  desc: string
}) {
  return (
    <Link
      to={to}
      className="group flex items-start gap-3 border border-border bg-card p-4 transition-colors hover:border-primary"
    >
      <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-secondary text-muted-foreground transition-colors group-hover:bg-primary/10 group-hover:text-primary">
        {icon}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium">{title}</p>
        <p className="text-sm text-muted-foreground">{desc}</p>
      </div>
      <ArrowRight className="size-4 shrink-0 text-muted-foreground/50 transition-transform group-hover:translate-x-0.5 group-hover:text-primary" />
    </Link>
  )
}

function EvidenceRow({ e }: { e: RecentEvidence }) {
  return (
    <div className="flex items-center gap-3 px-5 py-3.5">
      <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-secondary text-muted-foreground">
        <FileText className="size-4" />
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">
          {e.text || e.citation}
        </p>
        <p className="truncate text-xs text-muted-foreground">
          {e.system} · {e.citation}
          {e.unified_id ? ` · ${e.unified_id}` : ''}
        </p>
      </div>
    </div>
  )
}
