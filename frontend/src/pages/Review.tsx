import { useState } from 'react'
import { CheckCircle2, GitMerge, Loader2, Split, X } from 'lucide-react'

import { PageHeader } from '@/components/PageHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { useAsync } from '@/lib/useAsync'
import type { ReviewCandidate, ReviewItem } from '@/lib/types'

const KIND: Record<string, { label: string; variant: 'critical' | 'review' | 'linked' }> = {
  bridge_conflict: { label: 'Identifier conflict', variant: 'critical' },
  suggest_merge: { label: 'Suggested merge', variant: 'review' },
  review: { label: 'Weak match', variant: 'review' },
  anchor_confirm: { label: 'Confirm identifier', variant: 'linked' },
}

export function Review() {
  const { data, loading, error, reload } = useAsync<ReviewItem[]>((s) => api.review(s))
  const [busy, setBusy] = useState<number | null>(null)

  async function resolve(id: number, action: 'merge' | 'separate' | 'dismiss') {
    setBusy(id)
    try {
      await api.resolveReview(id, action)
      reload()
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <PageHeader
        eyebrow={data ? `${data.length} open item${data.length === 1 ? '' : 's'}` : 'Review queue'}
        title="Review queue"
        description="Nothing is auto-resolved when the evidence conflicts. Naming clashes, low-confidence merges, and unconfirmed identifiers surface here for a human."
      />

      {loading ? (
        <Card className="gap-3 py-5">
          <div className="space-y-3 px-5">
            <Skeleton className="h-6 w-2/3" />
            <Skeleton className="h-6 w-1/2" />
          </div>
        </Card>
      ) : error ? (
        <Card className="py-5">
          <p className="px-5 text-sm text-critical">
            Couldn't load the review queue. Is the backend running?
          </p>
        </Card>
      ) : !data || data.length === 0 ? (
        <Card className="py-12">
          <div className="flex flex-col items-center gap-2 px-5 text-center">
            <span className="flex size-11 items-center justify-center bg-verified-soft text-verified">
              <CheckCircle2 className="size-5" />
            </span>
            <p className="text-sm font-medium">All clear</p>
            <p className="max-w-xs text-sm text-muted-foreground">
              No conflicts or low-confidence merges need attention right now.
            </p>
          </div>
        </Card>
      ) : (
        <div className="space-y-3">
          {data.map((item) => (
            <ReviewRow
              key={item.id}
              item={item}
              busy={busy === item.id}
              onResolve={(action) => resolve(item.id, action)}
            />
          ))}
        </div>
      )}
    </>
  )
}

function ReviewRow({
  item,
  busy,
  onResolve,
}: {
  item: ReviewItem
  busy: boolean
  onResolve: (action: 'merge' | 'separate' | 'dismiss') => void
}) {
  const meta = KIND[item.kind] ?? { label: item.kind, variant: 'review' as const }
  const candidates = item.candidates ?? []
  const mergeLabel = item.kind === 'bridge_conflict' ? 'Merge anyway' : 'Confirm merge'

  return (
    <Card className="py-5">
      <div className="flex flex-col gap-3 px-5">
        <div className="flex items-center justify-between gap-3">
          <Badge variant={meta.variant}>{meta.label}</Badge>
          <span className="text-xs text-muted-foreground">#{item.id}</span>
        </div>
        <p className="text-sm leading-relaxed">{item.reason}</p>

        {/* Candidate records under review — what's actually being compared. */}
        {candidates.length > 0 && (
          <div className="grid gap-2 sm:grid-cols-2">
            {candidates.map((c) => (
              <CandidateCard key={c.record_id} c={c} />
            ))}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => onResolve('merge')}
          >
            {busy ? <Loader2 className="animate-spin" /> : <GitMerge />}
            {mergeLabel}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => onResolve('separate')}
          >
            <Split />
            Keep separate
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => onResolve('dismiss')}
          >
            <X />
            Dismiss
          </Button>
        </div>
      </div>
    </Card>
  )
}

function CandidateCard({ c }: { c: ReviewCandidate }) {
  const fields = Object.entries(c.fields ?? {}).slice(0, 5)
  return (
    <div className="rounded-xl border border-border/60 bg-card p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-xs font-medium text-muted-foreground">
          {c.system}
        </span>
        <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
          {c.record_id}
        </span>
      </div>
      {c.asset_name && (
        <p className="mt-1 truncate text-sm font-medium">{c.asset_name}</p>
      )}
      {c.text && (
        <p className="mt-0.5 line-clamp-2 text-xs text-foreground/80">{c.text}</p>
      )}
      {fields.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {fields.map(([k, v]) => (
            <span
              key={k}
              className="bg-secondary px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-secondary-foreground"
            >
              {k.replace(/_/g, ' ')}: {String(v)}
            </span>
          ))}
        </div>
      )}
      {c.unified_id && (
        <p className="mt-2 font-mono text-[11px] text-muted-foreground">
          → {c.unified_id}
        </p>
      )}
    </div>
  )
}
