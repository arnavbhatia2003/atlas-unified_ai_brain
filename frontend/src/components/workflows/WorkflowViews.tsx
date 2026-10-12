import { useEffect, useState } from 'react'
import { ArrowLeft, Clock, Loader2 } from 'lucide-react'

import { Badge, StatusDot } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Modal } from '@/components/ui/modal'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import type {
  ComplianceResult,
  Hypothesis,
  HistoryItem,
  RcaResult,
} from '@/lib/types'

function confidenceTone(c: number): 'review' | 'neutral' {
  if (c >= 0.4) return 'review'
  return 'neutral'
}

const POSTURE: Record<string, { label: string; tone: 'verified' | 'critical' | 'minor' }> = {
  compliant: { label: 'Compliant', tone: 'verified' },
  at_risk: { label: 'At risk', tone: 'critical' },
  unknown: { label: 'Unknown', tone: 'minor' },
}

/** Presentational RCA result (shared by the RCA page history + chat overlay). */
export function RcaReportView({ result }: { result: RcaResult }) {
  const report = result?.report
  const hypotheses = report?.hypotheses ?? []
  if (!report) {
    return (
      <p className="text-sm text-muted-foreground">
        {result?.message ?? 'This asset could not be resolved from the index.'}
      </p>
    )
  }
  return (
    <div className="space-y-4">
      <Badge variant="outline">{report.generation_mode === 'llm' ? 'Model hypotheses' : report.generation_mode === 'manual_evidence' ? 'Model unavailable · manual evidence' : report.generation_mode === 'no_evidence' ? 'No failure evidence' : 'Stored analysis · generation mode unrecorded'}</Badge>
      <p className="text-sm leading-relaxed">{report.summary}</p>

      {hypotheses.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs font-medium text-muted-foreground">
            Hypotheses for review · scores are heuristics, not probabilities
          </p>
          <div className="divide-y divide-border/60 rounded-xl border border-border/60">
            {hypotheses.map((h, i) => (
              <HypothesisRow key={i} h={h} />
            ))}
          </div>
        </div>
      )}

      {report.contradictions.length > 0 && (
        <UncertaintyBlock tone="critical" title="Contradicting evidence" items={report.contradictions} />
      )}
      {report.unresolved.length > 0 && (
        <UncertaintyBlock tone="minor" title="Unresolved — open questions" items={report.unresolved} />
      )}
      {result.evidence_from && (
        <Badge variant="outline">Evidence from {result.evidence_from}</Badge>
      )}
    </div>
  )
}

function HypothesisRow({ h }: { h: Hypothesis }) {
  const tone = confidenceTone(h.confidence)
  return (
    <div className="px-4 py-3">
      <div className="flex items-start justify-between gap-3">
        <h4 className="text-sm font-semibold">{h.cause}</h4>
        <Badge variant={tone === 'neutral' ? 'neutral' : tone} className="font-mono">
          {h.confidence.toFixed(2)}
        </Badge>
      </div>
      <p className="mt-1 text-sm text-muted-foreground">{h.explanation}</p>
      {h.evidence.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {h.evidence.map((e, i) => (
            <Badge key={i} variant="linked" className="font-mono">
              {e}
            </Badge>
          ))}
        </div>
      )}
    </div>
  )
}

/** Presentational Compliance result. */
export function ComplianceReportView({ result }: { result: ComplianceResult }) {
  const narrative = result?.narrative
  const posture = POSTURE[narrative?.posture ?? 'unknown'] ?? POSTURE.unknown
  const atRisk = result?.at_risk_assets ?? []
  return (
    <div className="space-y-4">
      <Badge variant="outline">{narrative?.generation_mode === 'llm' ? 'Model summary · record-based checks' : narrative?.generation_mode === 'manual_evidence' ? 'Model unavailable · record-based checks' : narrative?.generation_mode === 'no_evidence' ? 'No linked rule evidence' : 'Stored summary · generation mode unrecorded'}</Badge>
      <div className="flex items-center gap-2">
        <StatusDot tone={posture.tone} />
        <span className="text-sm font-semibold">{posture.label}</span>
      </div>
      {narrative?.summary && <p className="text-sm leading-relaxed">{narrative.summary}</p>}

      <div>
        <p className="mb-2 text-xs font-medium text-muted-foreground">
          At-risk assets · computed from records ({atRisk.length})
        </p>
        {atRisk.length === 0 ? (
          <p className="text-sm text-muted-foreground">{result.assessed_rule_links ? 'No unmet linked obligations found in these records. This does not certify compliance.' : 'No recorded obligations were assessed; compliance is unknown.'}</p>
        ) : (
          <ul className="space-y-1.5">
            {atRisk.map((a, i) => (
              <li key={i} className="rounded-lg bg-critical-soft px-3 py-2 text-sm">
                <span className="font-mono text-xs">{a.asset}</span> · {a.rule}
                <p className="text-xs text-muted-foreground">{a.reason}</p>
              </li>
            ))}
          </ul>
        )}
      </div>

      {narrative && narrative.contradictions.length > 0 && (
        <UncertaintyBlock tone="critical" title="Contradicting evidence" items={narrative.contradictions} />
      )}
      {narrative && narrative.unresolved.length > 0 && (
        <UncertaintyBlock tone="minor" title="Unresolved — open questions" items={narrative.unresolved} />
      )}
      {result.evidence_from && (
        <Badge variant="outline">Evidence from {result.evidence_from}</Badge>
      )}
    </div>
  )
}

function UncertaintyBlock({
  tone,
  title,
  items,
}: {
  tone: 'critical' | 'minor'
  title: string
  items: string[]
}) {
  return (
    <div>
      <p
        className={cn(
          'mb-2 flex items-center gap-1.5 text-xs font-medium',
          tone === 'critical' ? 'text-critical' : 'text-minor',
        )}
      >
        <StatusDot tone={tone} />
        {title}
      </p>
      <ul className="space-y-1.5">
        {items.map((it, i) => (
          <li
            key={i}
            className={cn(
              'rounded-lg px-3 py-2 text-sm text-foreground/80',
              tone === 'critical' ? 'bg-critical-soft' : 'bg-minor-soft',
            )}
          >
            {it}
          </li>
        ))}
      </ul>
    </div>
  )
}

/** History dialog: lists past runs of `kind`; opening one shows its result. */
export function HistoryDialog({
  kind,
  open,
  onClose,
}: {
  kind: 'rca' | 'compliance'
  open: boolean
  onClose: () => void
}) {
  const [items, setItems] = useState<HistoryItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<{ item: HistoryItem; result: RcaResult | ComplianceResult } | null>(null)
  const [loadingRun, setLoadingRun] = useState(false)

  useEffect(() => {
    if (!open) return
    setItems(null)
    setError(null)
    setSelected(null)
    const controller = new AbortController()
    api
      .history(kind, controller.signal)
      .then(setItems)
      .catch((e) => {
        if (!(e instanceof DOMException && e.name === 'AbortError'))
          setError(e instanceof Error ? e.message : 'Failed to load history')
      })
    return () => controller.abort()
  }, [open, kind])

  async function openRun(item: HistoryItem) {
    setLoadingRun(true)
    try {
      const run = await api.historyRun(item.id)
      setSelected({ item, result: run.result })
    } catch {
      setError('Could not load that run.')
    } finally {
      setLoadingRun(false)
    }
  }

  const title = kind === 'rca' ? 'Root-cause history' : 'Compliance history'

  return (
    <Modal open={open} onClose={onClose} title={selected ? undefined : title}>
      {selected ? (
        <div className="space-y-4">
          <Button variant="outline" size="sm" onClick={() => setSelected(null)}>
            <ArrowLeft />
            Back to history
          </Button>
          <div>
            <p className="text-sm font-medium">
              {selected.item.question || (kind === 'rca' ? 'General analysis' : 'Posture review')}
            </p>
            <p className="text-xs text-muted-foreground">
              {selected.item.created_at?.replace('T', ' ').slice(0, 16)}
              {selected.item.asset ? ` · ${selected.item.asset}` : ''}
              {selected.item.rule ? ` · ${selected.item.rule}` : ''}
            </p>
          </div>
          {kind === 'rca' ? (
            <RcaReportView result={selected.result as RcaResult} />
          ) : (
            <ComplianceReportView result={selected.result as ComplianceResult} />
          )}
        </div>
      ) : error ? (
        <p className="text-sm text-critical">{error}</p>
      ) : !items ? (
        <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading history…
        </div>
      ) : items.length === 0 ? (
        <p className="py-4 text-sm text-muted-foreground">
          No past {kind === 'rca' ? 'analyses' : 'reviews'} yet.
        </p>
      ) : (
        <ul className="divide-y divide-border/60">
          {items.map((it) => (
            <li key={it.id}>
              <button
                onClick={() => openRun(it)}
                disabled={loadingRun}
                className="flex w-full items-start gap-3 py-3 text-left transition-colors hover:bg-secondary/50 disabled:opacity-60"
              >
                <Clock className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">
                    {it.question || (kind === 'rca' ? 'General analysis' : 'Posture review')}
                  </p>
                  <p className="truncate text-xs text-muted-foreground">
                    {it.created_at?.replace('T', ' ').slice(0, 16)}
                    {it.posture ? ` · ${it.posture}` : ''}
                    {it.asset ? ` · ${it.asset}` : ''}
                    {it.rule ? ` · ${it.rule}` : ''}
                  </p>
                  {it.summary && (
                    <p className="mt-0.5 line-clamp-2 text-xs text-foreground/70">
                      {it.summary}
                    </p>
                  )}
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  )
}
