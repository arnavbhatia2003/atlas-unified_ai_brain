import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeft, History, Loader2, Play, Square } from 'lucide-react'

import { HistoryDialog } from '@/components/workflows/WorkflowViews'
import { Badge, StatusDot } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { streamWorkflow } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { Hypothesis, RcaResult, WorkflowEvent } from '@/lib/types'

const STAGES = ['resolve', 'gather', 'reason', 'complete'] as const
const STAGE_LABEL: Record<string, string> = {
  resolve: 'Resolve asset',
  gather: 'Evidence assembly',
  reason: 'Hypothesis scoring',
  complete: 'Final chain',
}

function confidenceTone(c: number): 'verified' | 'review' | 'neutral' {
  if (c >= 0.7) return 'verified'
  if (c >= 0.4) return 'review'
  return 'neutral'
}

export function Rca() {
  const [asset, setAsset] = useState('')
  const [question, setQuestion] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [reached, setReached] = useState<Set<string>>(new Set())
  const [status, setStatus] = useState('')
  const [result, setResult] = useState<RcaResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  async function run() {
    if (!asset.trim() || streaming) return
    setStreaming(true)
    setResult(null)
    setError(null)
    setReached(new Set())
    setStatus('Starting root-cause analysis')
    const controller = new AbortController()
    abortRef.current = controller
    try {
      for await (const ev of streamWorkflow(
        '/api/rca',
        { question: question.trim(), asset: asset.trim() },
        controller.signal,
      ) as AsyncGenerator<WorkflowEvent>) {
        if (ev.step && STAGES.includes(ev.step as never)) {
          setReached((prev) => new Set(prev).add(ev.step))
        }
        if (ev.step === 'complete') setResult((ev.result as RcaResult) ?? null)
        if (ev.message) setStatus(ev.message)
      }
    } catch (e) {
      if (!(e instanceof DOMException && e.name === 'AbortError')) {
        setError(e instanceof Error ? e.message : 'Request failed')
      }
    } finally {
      setStreaming(false)
      abortRef.current = null
    }
  }

  const report = result?.report
  const hypotheses = report?.hypotheses ?? []

  return (
    <>
      <div className="mb-6 flex items-center justify-between gap-3">
        <Button variant="outline" size="sm" asChild>
          <Link to="/workflows">
            <ArrowLeft />
            Back to Workflows
          </Link>
        </Button>
        <Button variant="outline" size="sm" onClick={() => setHistoryOpen(true)}>
          <History />
          History
        </Button>
      </div>

      <div className="mb-6">
        <Badge variant="review">Root-cause analysis</Badge>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight sm:text-[1.75rem]">
          Root cause analysis
        </h1>
        <p className="mt-1.5 max-w-2xl text-sm leading-relaxed text-muted-foreground">
          Enter the asset you want diagnosed. Analysis runs only against the
          failure evidence linked to it — evidence-backed hypotheses first,
          unresolved links kept visible, never dropped to look cleaner.
        </p>
      </div>

      {/* Composer */}
      <Card className="mb-6 gap-4 py-5">
        <div className="grid gap-3 px-5 sm:grid-cols-[1fr_1.4fr_auto] sm:items-end">
          <label className="text-sm">
            <span className="mb-1.5 block font-medium">Asset to diagnose</span>
            <Input
              value={asset}
              onChange={(e) => setAsset(e.target.value)}
              placeholder="e.g. PMP-101, a tag, or an asset id"
              disabled={streaming}
            />
          </label>
          <label className="text-sm">
            <span className="mb-1.5 block font-medium">
              Narrow the question{' '}
              <span className="font-normal text-muted-foreground">— optional</span>
            </span>
            <Input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="e.g. Why did the bearing overheat on 12 Mar?"
              disabled={streaming}
            />
          </label>
          <Button onClick={run} disabled={streaming || !asset.trim()}>
            {streaming ? <Loader2 className="animate-spin" /> : <Play />}
            Run analysis
          </Button>
        </div>
        <p className="px-5 text-xs text-muted-foreground">
          Leaving the question blank runs a general root-cause read of the linked
          evidence. It does not assume the asset has failed — if there is no
          failure evidence, the analysis says so plainly.
        </p>
      </Card>

      <HistoryDialog kind="rca" open={historyOpen} onClose={() => setHistoryOpen(false)} />

      {/* Stage rail */}
      {(streaming || result) && (
        <div className="mb-6 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {STAGES.map((s, i) => {
            const done = reached.has(s)
            const active = streaming && !result && lastReached(reached) === s
            return (
              <div
                key={s}
                className={cn(
                  'flex items-center gap-2 rounded-xl border px-3 py-2.5 text-sm font-medium',
                  active
                    ? 'border-primary/40 bg-primary/5 text-primary'
                    : done
                      ? 'border-verified/30 bg-verified-soft/50 text-foreground'
                      : 'border-border text-muted-foreground',
                )}
              >
                <span
                  className={cn(
                    'flex size-5 items-center justify-center text-xs',
                    done
                      ? 'bg-verified text-background'
                      : active
                        ? 'bg-primary text-background'
                        : 'bg-secondary text-muted-foreground',
                  )}
                >
                  {i + 1}
                </span>
                {STAGE_LABEL[s]}
              </div>
            )
          })}
        </div>
      )}

      {error && (
        <Card className="mb-6 py-4">
          <p className="px-5 text-sm text-critical">{error}</p>
        </Card>
      )}

      {(streaming || result) && (
        <div className="grid gap-5 lg:grid-cols-[1.5fr_1fr]">
          {/* Hypotheses */}
          <Card className="py-5">
            <div className="flex items-center justify-between px-5">
              <h2 className="text-base font-semibold">Evidence-backed hypotheses</h2>
              {streaming && (
                <span className="flex items-center gap-1.5 text-xs font-medium text-primary">
                  <StatusDot className="motion-safe:animate-pulse" />
                  {status}
                </span>
              )}
            </div>
            <div className="mt-2 divide-y divide-border/60">
              {hypotheses.length > 0
                ? hypotheses.map((h, i) => <HypothesisRow key={i} h={h} />)
                : !streaming &&
                  result && (
                    <p className="px-5 py-4 text-sm text-muted-foreground">
                      {report?.summary ??
                        'No evidence-backed hypotheses could be produced.'}
                    </p>
                  )}
              {streaming && hypotheses.length === 0 && (
                <p className="px-5 py-4 text-sm text-muted-foreground">
                  {status}…
                </p>
              )}
            </div>
          </Card>

          {/* Synthesis / uncertainty */}
          <Card className="py-5">
            <div className="flex items-center justify-between px-5">
              <h2 className="text-base font-semibold">Causal synthesis</h2>
              {streaming && (
                <Button variant="outline" size="sm" onClick={() => abortRef.current?.abort()}>
                  <Square className="fill-current" />
                  Stop
                </Button>
              )}
            </div>
            <div className="mt-3 space-y-4 px-5">
              {report ? (
                <>
                  <p className="text-sm leading-relaxed">{report.summary}</p>
                  {report.contradictions.length > 0 && (
                    <UncertaintyBlock
                      tone="critical"
                      title="Contradicting evidence"
                      items={report.contradictions}
                    />
                  )}
                  {report.unresolved.length > 0 && (
                    <UncertaintyBlock
                      tone="minor"
                      title="Unresolved — preserved as open questions"
                      items={report.unresolved}
                    />
                  )}
                  {result?.evidence_from && (
                    <Badge variant="outline">Evidence from {result.evidence_from}</Badge>
                  )}
                </>
              ) : (
                <p className="text-sm text-muted-foreground">
                  Advancing the stages to issue a grounded causal chain…
                </p>
              )}
            </div>
          </Card>
        </div>
      )}
    </>
  )
}

function HypothesisRow({ h }: { h: Hypothesis }) {
  const tone = confidenceTone(h.confidence)
  return (
    <div className="px-5 py-4">
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-semibold">{h.cause}</h3>
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

function lastReached(reached: Set<string>): string | null {
  let last: string | null = null
  for (const s of STAGES) if (reached.has(s)) last = s
  return last
}
