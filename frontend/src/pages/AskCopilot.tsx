import { useEffect, useRef, useState } from 'react'
import { ArrowRight, ChevronDown, History, Loader2, Maximize2, Square } from 'lucide-react'

import {
  ComplianceReportView,
  HistoryDialog,
  RcaReportView,
} from '@/components/workflows/WorkflowViews'
import { Badge, StatusDot } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Modal } from '@/components/ui/modal'
import { streamWorkflow } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { ChatResult, Citation, ComplianceResult, RcaResult } from '@/lib/types'

const EXAMPLES = [
  'Summarize the most-referenced asset and flag any contradicting evidence.',
  'What recent incidents have been logged, and which assets do they affect?',
]

interface UserTurn {
  role: 'user'
  id: string
  text: string
}
interface AssistantTurn {
  role: 'assistant'
  id: string
  thinking: string
  thoughtMs: number | null
  answer: string
  result: ChatResult | null
  status: string
  error: string | null
  streaming: boolean
}
type Turn = UserTurn | AssistantTurn

const uid = () => Math.random().toString(36).slice(2)

export function AskCopilot() {
  const [input, setInput] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  // A routed RCA/Compliance result opens as a workflow window over the chat.
  const [workflow, setWorkflow] = useState<ChatResult | null>(null)
  const [historyKind, setHistoryKind] = useState<'rca' | 'compliance' | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const thinkStartRef = useRef<number | null>(null)

  const streaming = turns.some((t) => t.role === 'assistant' && t.streaming)
  const hasThread = turns.length > 0

  // Stick to the bottom while streaming, unless the user has scrolled up.
  const scrollRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const [stick, setStick] = useState(true)
  useEffect(() => {
    if (stick) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [turns, stick])

  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 80)
  }

  function patchAssistant(id: string, patch: (t: AssistantTurn) => AssistantTurn) {
    setTurns((ts) =>
      ts.map((t) => (t.role === 'assistant' && t.id === id ? patch(t) : t)),
    )
  }

  async function submit() {
    const message = input.trim()
    if (!message || streaming) return
    setInput('')
    setStick(true)
    const aId = uid()
    setTurns((ts) => [
      ...ts,
      { role: 'user', id: uid(), text: message },
      {
        role: 'assistant',
        id: aId,
        thinking: '',
        thoughtMs: null,
        answer: '',
        result: null,
        status: 'Routing your question',
        error: null,
        streaming: true,
      },
    ])
    const controller = new AbortController()
    abortRef.current = controller
    thinkStartRef.current = null
    try {
      for await (const ev of streamWorkflow('/api/chat', { message }, controller.signal)) {
        if (ev.step === 'thinking') {
          if (thinkStartRef.current == null) thinkStartRef.current = Date.now()
          patchAssistant(aId, (t) => ({ ...t, thinking: t.thinking + (ev.text ?? '') }))
        } else if (ev.step === 'token') {
          const elapsed =
            thinkStartRef.current != null ? Date.now() - thinkStartRef.current : null
          patchAssistant(aId, (t) => ({
            ...t,
            thoughtMs: t.thoughtMs ?? elapsed,
            answer: t.answer + (ev.text ?? ''),
          }))
        } else if (ev.step === 'complete') {
          const r = (ev.result as ChatResult) ?? null
          patchAssistant(aId, (t) => ({ ...t, result: r }))
          if (r && (r.intent === 'rca' || r.intent === 'compliance')) setWorkflow(r)
        } else if (ev.message) {
          patchAssistant(aId, (t) => ({ ...t, status: ev.message as string }))
        }
      }
    } catch (e) {
      if (!(e instanceof DOMException && e.name === 'AbortError')) {
        const msg = e instanceof Error ? e.message : 'Request failed'
        patchAssistant(aId, (t) => ({ ...t, error: msg }))
      }
    } finally {
      patchAssistant(aId, (t) => ({ ...t, streaming: false }))
      abortRef.current = null
      thinkStartRef.current = null
    }
  }

  function interrupt() {
    abortRef.current?.abort()
  }

  const composer = (
    <Composer
      value={input}
      onChange={setInput}
      onSubmit={submit}
      streaming={streaming}
    />
  )

  return (
    <>
      {!hasThread ? (
        // Empty state: centered composer + quick questions (Claude-style).
        <div className="flex min-h-[62svh] flex-col items-center justify-center">
          <div className="w-full max-w-2xl">
            {composer}
            <div className="mt-3 flex flex-wrap justify-center gap-2">
              {EXAMPLES.map((ex) => (
                <button
                  key={ex}
                  onClick={() => setInput(ex)}
                  className="border border-border bg-card px-3 py-1.5 font-mono text-[11px] uppercase tracking-wider text-muted-foreground transition-colors hover:border-primary hover:text-foreground"
                >
                  {ex.length > 48 ? ex.slice(0, 48) + '…' : ex}
                </button>
              ))}
            </div>
          </div>
        </div>
      ) : (
        // Active thread: scrollable messages above, composer pinned at bottom.
        <div className="flex h-[calc(100dvh-10rem)] flex-col md:h-[calc(100dvh-4.5rem)]">
          <div
            ref={scrollRef}
            onScroll={onScroll}
            className="flex-1 overflow-y-auto pb-2"
          >
            <div className="mx-auto max-w-3xl space-y-5">
              {turns.map((t) =>
                t.role === 'user' ? (
                  <UserBubble key={t.id} text={t.text} />
                ) : (
                  <AssistantMessage
                    key={t.id}
                    turn={t}
                    onInterrupt={interrupt}
                    onOpenWorkflow={setWorkflow}
                    onHistory={setHistoryKind}
                  />
                ),
              )}
              <div ref={bottomRef} />
            </div>
          </div>
          <div className="shrink-0 pt-3">
            <div className="mx-auto max-w-3xl">{composer}</div>
          </div>
        </div>
      )}

      {/* Workflow window overlaid on the chat background (RCA / compliance) */}
      <Modal
        open={!!workflow}
        onClose={() => setWorkflow(null)}
        title={workflow?.intent === 'rca' ? 'Root cause analysis' : 'Compliance review'}
        className="sm:max-w-2xl"
      >
        {workflow && (
          <div className="space-y-4">
            {workflow.intent === 'rca' ? (
              <RcaReportView result={workflow as unknown as RcaResult} />
            ) : (
              <ComplianceReportView result={workflow as unknown as ComplianceResult} />
            )}
            <div className="flex flex-wrap gap-2 border-t border-border/60 pt-4">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setHistoryKind(workflow.intent as 'rca' | 'compliance')}
              >
                <History />
                Previous runs
              </Button>
              <Button variant="ghost" size="sm" asChild>
                <a href={workflow.intent === 'rca' ? '/workflows/rca' : '/workflows/compliance'}>
                  Open full workflow page
                  <ArrowRight />
                </a>
              </Button>
            </div>
          </div>
        )}
      </Modal>

      {historyKind && (
        <HistoryDialog
          kind={historyKind}
          open={!!historyKind}
          onClose={() => setHistoryKind(null)}
        />
      )}
    </>
  )
}

/** Single composer box: borderless textarea + send button in one container. */
function Composer({
  value,
  onChange,
  onSubmit,
  streaming,
}: {
  value: string
  onChange: (v: string) => void
  onSubmit: () => void
  streaming: boolean
}) {
  return (
    <div className="rounded-2xl border border-border bg-card p-2 shadow-soft transition focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/30">
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            onSubmit()
          }
        }}
        placeholder="Ask about an asset, a failure, a permit clause, or how records connect…"
        rows={2}
        className="w-full resize-none bg-transparent px-3 py-2.5 text-sm leading-relaxed text-foreground outline-none placeholder:text-muted-foreground"
      />
      <div className="flex items-center justify-end px-1.5 pb-1.5">
        <Button onClick={onSubmit} disabled={streaming || !value.trim()}>
          {streaming ? <Loader2 className="animate-spin" /> : <ArrowRight />}
          Ask Copilot
        </Button>
      </div>
    </div>
  )
}

/** User message — right-aligned bubble in the accent colour. */
function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-primary px-4 py-2.5 text-sm leading-relaxed text-primary-foreground">
        {text}
      </div>
    </div>
  )
}

/** Assistant message — left-aligned card-surface bubble with rich content. */
function AssistantMessage({
  turn,
  onInterrupt,
  onOpenWorkflow,
  onHistory,
}: {
  turn: AssistantTurn
  onInterrupt: () => void
  onOpenWorkflow: (r: ChatResult) => void
  onHistory: (k: 'rca' | 'compliance') => void
}) {
  const { result } = turn
  return (
    <div className="flex justify-start">
      <div className="w-full max-w-[90%] space-y-3 rounded-2xl rounded-bl-md border border-border/60 bg-card px-4 py-3 shadow-soft md:max-w-[85%]">
        <ThinkingTrace
          text={turn.thinking}
          active={turn.streaming && !turn.answer}
          thoughtMs={turn.thoughtMs}
        />

        {turn.error ? (
          <p className="text-sm text-critical">{turn.error}</p>
        ) : (
          <>
            {turn.streaming && !turn.answer && !turn.thinking && (
              <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
                <StatusDot className="motion-safe:animate-pulse" />
                {turn.status}…
              </p>
            )}
            {turn.answer && <AnswerBody text={turn.answer} />}
            {!turn.streaming && result && !turn.answer && <RoutedResult result={result} />}
          </>
        )}

        {turn.streaming && (
          <Button variant="outline" size="sm" onClick={onInterrupt}>
            <Square className="fill-current" />
            Interrupt
          </Button>
        )}

        {!turn.streaming &&
          result &&
          (result.intent === 'ask' || result.intent === 'asset_lookup') && (
            <AssessmentDetails result={result} />
          )}

        {!turn.streaming &&
          result &&
          (result.intent === 'rca' || result.intent === 'compliance') && (
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm" onClick={() => onOpenWorkflow(result)}>
                <Maximize2 />
                {result.intent === 'rca' ? 'Open root-cause window' : 'Open compliance window'}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onHistory(result.intent as 'rca' | 'compliance')}
              >
                <History />
                History
              </Button>
            </div>
          )}
      </div>
    </div>
  )
}

/** Collapsible live reasoning trace (streams before the answer, then collapses). */
function ThinkingTrace({
  text,
  active,
  thoughtMs,
}: {
  text: string
  active: boolean
  thoughtMs: number | null
}) {
  const [open, setOpen] = useState(true)
  const userToggled = useRef(false)
  useEffect(() => {
    if (!active && !userToggled.current) setOpen(false)
  }, [active])
  const bodyRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (open && active && bodyRef.current)
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight
  }, [text, open, active])

  if (!text) return null
  const label = active
    ? 'Thinking'
    : thoughtMs != null
      ? `Thought for ${(thoughtMs / 1000).toFixed(1)}s`
      : 'Thought process'

  return (
    <div className="rounded-xl border border-border/60 bg-secondary/40">
      <button
        onClick={() => {
          userToggled.current = true
          setOpen((o) => !o)
        }}
        className="flex w-full items-center justify-between gap-2 px-3.5 py-2.5 text-left"
        aria-expanded={open}
      >
        <span className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
          <StatusDot className={cn(active && 'motion-safe:animate-pulse')} />
          {label}
        </span>
        <ChevronDown
          className={cn(
            'size-4 text-muted-foreground transition-transform duration-200',
            open && 'rotate-180',
          )}
        />
      </button>
      {open && (
        <div
          ref={bodyRef}
          className="max-h-52 overflow-auto border-t border-border/50 px-3.5 py-3"
        >
          <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-muted-foreground/90">
            {text}
          </p>
        </div>
      )}
    </div>
  )
}

/** Renders answer prose, turning [citation] markers into small chips. */
function AnswerBody({ text }: { text: string }) {
  const parts = text.split(/(\[[^\]]+\])/g)
  return (
    <p className="whitespace-pre-wrap text-sm leading-7 text-foreground/90">
      {parts.map((p, i) =>
        /^\[[^\]]+\]$/.test(p) ? (
          <span
            key={i}
            className="mx-0.5 inline-flex items-center rounded-md bg-linked-soft px-1.5 py-0.5 align-baseline font-mono text-[11px] font-medium text-linked"
          >
            {p.slice(1, -1)}
          </span>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </p>
  )
}

function RoutedResult({ result }: { result: ChatResult }) {
  if (result.intent === 'rca' && result.report) {
    return (
      <div className="space-y-2">
        <p className="text-sm leading-relaxed">{result.report.summary}</p>
      </div>
    )
  }
  if (result.intent === 'compliance' && result.narrative) {
    return <p className="text-sm leading-relaxed">{result.narrative.summary}</p>
  }
  if (result.intent === 'asset_lookup') {
    return <p className="text-sm leading-relaxed">{result.answer ?? 'Asset found.'}</p>
  }
  if (result.intent === 'overview') {
    return (
      <div className="space-y-3">
        <p className="text-sm leading-relaxed">{result.answer}</p>
        {result.assets && result.assets.length > 0 && (
          <ul className="divide-y divide-border/60 rounded-lg border border-border/60">
            {result.assets.map((a) => (
              <li
                key={a.unified_id}
                className="flex items-center justify-between gap-3 px-3 py-2 text-sm"
              >
                <span className="truncate">{a.asset_name || a.unified_id}</span>
                <span className="flex shrink-0 items-center gap-2">
                  {a.needs_review && <Badge variant="review">review</Badge>}
                  <span className="font-mono text-xs text-muted-foreground">
                    {a.unified_id}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    )
  }
  return <p className="text-sm text-muted-foreground">No answer produced.</p>
}

function AssessmentDetails({ result }: { result: ChatResult }) {
  const [open, setOpen] = useState(false)
  const citations = normalizeCitations(result.citations)
  const contradictions = result.contradictions ?? []

  return (
    <div className="border-t border-border/60 pt-3">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between text-sm font-medium"
      >
        Sources & contradictions
        <ChevronDown
          className={cn('size-4 text-muted-foreground transition-transform', open && 'rotate-180')}
        />
      </button>

      {open && (
        <div className="mt-4 space-y-5">
          {citations.length > 0 && (
            <div>
              <p className="mb-2 text-xs font-medium text-muted-foreground">
                Sources · {citations.length} checked
                {contradictions.length > 0 &&
                  ` · ${contradictions.length} contradiction${contradictions.length === 1 ? '' : 's'}`}
              </p>
              <div className="flex flex-wrap gap-2">
                {citations.map((c) => (
                  <Badge key={c.id} variant="linked" className="font-mono">
                    {c.id}
                    {c.similarity > 0 && (
                      <span className="opacity-60">{Math.round(c.similarity * 100)}%</span>
                    )}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          {contradictions.length > 0 && (
            <div>
              <p className="mb-2 flex items-center gap-1.5 text-xs font-medium text-critical">
                <StatusDot tone="critical" />
                Contradicting / unresolved evidence
              </p>
              <ul className="space-y-1.5">
                {contradictions.map((c, i) => (
                  <li
                    key={i}
                    className="rounded-lg bg-critical-soft px-3 py-2 text-sm text-foreground/80"
                  >
                    {c}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function normalizeCitations(c: ChatResult['citations']): Citation[] {
  if (!c) return []
  return c.map((item) =>
    typeof item === 'string'
      ? { id: item, unified_id: null, system: '', similarity: 0 }
      : item,
  )
}
