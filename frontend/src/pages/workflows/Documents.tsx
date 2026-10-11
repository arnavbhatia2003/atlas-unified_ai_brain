import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowLeft,
  FileText,
  FolderUp,
  Loader2,
  RefreshCw,
  Square,
  Upload,
} from 'lucide-react'

import { Badge, StatusDot } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { api, startDocumentUpload, streamJob } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { DocumentParse } from '@/lib/types'

const JOB_KEY = 'atlas.ingestJobId'

interface LogLine {
  step: string
  message: string
  tone: 'info' | 'good' | 'warn' | 'bad'
}

function toneFor(step: string): LogLine['tone'] {
  if (step === 'extracted' || step === 'complete' || step === 'done') return 'good'
  if (step === 'skip') return 'warn'
  if (step === 'doc_error' || step === 'error' || step === 'cancelled') return 'bad'
  return 'info'
}

function statusTone(status: string): 'verified' | 'review' | 'critical' | 'neutral' {
  if (status === 'processed') return 'verified'
  if (status === 'error') return 'critical'
  if (status === 'parsed') return 'review'
  return 'neutral'
}

export function Documents() {
  const [files, setFiles] = useState<File[]>([])
  const [folder, setFolder] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [log, setLog] = useState<LogLine[]>([])
  const [status, setStatus] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [docs, setDocs] = useState<DocumentParse[]>([])
  const jobIdRef = useRef<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const logEndRef = useRef<HTMLDivElement>(null)

  async function refreshDocs() {
    try {
      setDocs(await api.documents())
    } catch {
      /* ignore */
    }
  }

  // Subscribe (or re-subscribe) to a background job's status stream. The stream
  // replays the full buffered log then follows the live tail, so returning to
  // this page after navigating away shows the job still running.
  async function subscribe(jobId: string) {
    // detach any previous subscription (does NOT stop the server job)
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    jobIdRef.current = jobId
    localStorage.setItem(JOB_KEY, jobId)
    setStreaming(true)
    setError(null)
    setLog([])
    setStatus('Connecting to ingestion job…')
    try {
      for await (const ev of streamJob(jobId, controller.signal)) {
        const step = String(ev.step || 'status')
        if (step === 'heartbeat') continue
        if (step === '_end') break
        const msg = ev.message || ''
        if (msg) setStatus(msg)
        setLog((prev) => [...prev, { step, message: msg, tone: toneFor(step) }])
      }
      // stream ended → job finished
      localStorage.removeItem(JOB_KEY)
      jobIdRef.current = null
      setStreaming(false)
      refreshDocs()
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') return // detached
      setError(e instanceof Error ? e.message : 'Lost connection to the job')
      setStreaming(false)
    }
  }

  useEffect(() => {
    refreshDocs()
    // Resume a job still running from a previous visit (localStorage or server).
    ;(async () => {
      let jid = localStorage.getItem(JOB_KEY)
      if (!jid) {
        try {
          const { jobs } = await api.documentJobs()
          jid = jobs.find((j) => j.status === 'running')?.id ?? null
        } catch {
          /* ignore */
        }
      }
      if (jid) subscribe(jid)
    })()
    // On unmount, detach the viewer only — the job keeps running server-side.
    return () => abortRef.current?.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [log])

  async function runUpload() {
    if (!files.length || streaming) return
    try {
      const job = await startDocumentUpload(files)
      if (job.job_id) subscribe(job.job_id)
      else setError(job.error || 'Could not start ingestion')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Upload failed')
    }
  }

  async function runFolder() {
    if (!folder.trim() || streaming) return
    try {
      const job = await api.startIngestPath(folder.trim())
      if (job.job_id) subscribe(job.job_id)
      else setError(job.error || 'Could not start ingestion')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Ingestion failed')
    }
  }

  async function stopJob() {
    const jid = jobIdRef.current
    if (jid) {
      try {
        await api.cancelDocumentJob(jid)
      } catch {
        /* ignore */
      }
    }
  }

  return (
    <>
      <div className="mb-6 flex items-center justify-between gap-3">
        <Button variant="outline" size="sm" asChild>
          <Link to="/workflows">
            <ArrowLeft />
            Back to Workflows
          </Link>
        </Button>
        <Button variant="outline" size="sm" onClick={refreshDocs}>
          <RefreshCw />
          Refresh
        </Button>
      </div>

      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight sm:text-[1.75rem]">
          Ingest documents (PDF)
        </h1>
      </div>

      {/* Upload */}
      <Card className="mb-4 gap-4 py-5">
        <div className="px-5">
          <h2 className="text-base font-semibold">Upload PDFs</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Select one or more PDFs from your computer.
          </p>
        </div>
        <div className="flex flex-col gap-3 px-5 sm:flex-row sm:items-center">
          <label
            className={cn(
              'flex flex-1 cursor-pointer items-center gap-3 rounded-xl border border-dashed',
              'border-border px-4 py-3 text-sm transition-colors hover:border-primary/40',
              streaming && 'pointer-events-none opacity-60',
            )}
          >
            <Upload className="size-4 text-muted-foreground" />
            <span className="text-muted-foreground">
              {files.length
                ? `${files.length} file${files.length === 1 ? '' : 's'} selected`
                : 'Choose PDF files…'}
            </span>
            <input
              type="file"
              accept="application/pdf,.pdf"
              multiple
              className="hidden"
              disabled={streaming}
              onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
            />
          </label>
          <Button onClick={runUpload} disabled={streaming || files.length === 0}>
            {streaming ? <Loader2 className="animate-spin" /> : <Upload />}
            Ingest {files.length || ''} document{files.length === 1 ? '' : 's'}
          </Button>
        </div>
      </Card>

      {/* Folder path (local dev) */}
      <Card className="mb-6 gap-4 py-5">
        <div className="px-5">
          <h2 className="text-base font-semibold">Ingest a server folder</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Reads PDFs from a path on the machine running the backend. Handy for a
            whole dataset folder in local development.
          </p>
        </div>
        <div className="flex flex-col gap-3 px-5 sm:flex-row sm:items-end">
          <label className="flex-1 text-sm">
            <span className="mb-1.5 block font-medium">Folder or file path</span>
            <Input
              value={folder}
              onChange={(e) => setFolder(e.target.value)}
              placeholder="C:\\path\\to\\pdfs"
              disabled={streaming}
              className="font-mono text-xs"
            />
          </label>
          <Button variant="outline" onClick={runFolder} disabled={streaming || !folder.trim()}>
            {streaming ? <Loader2 className="animate-spin" /> : <FolderUp />}
            Ingest folder
          </Button>
        </div>
      </Card>

      {error && (
        <Card className="mb-6 py-4">
          <p className="px-5 text-sm text-critical">{error}</p>
        </Card>
      )}

      {/* Live status */}
      {(streaming || log.length > 0) && (
        <Card className="mb-6 py-5">
          <div className="flex items-center justify-between px-5">
            <div className="flex items-center gap-2">
              <h2 className="text-base font-semibold">Ingestion status</h2>
              {streaming && (
                <span className="flex items-center gap-1.5 text-xs font-medium text-primary">
                  <StatusDot className="motion-safe:animate-pulse" />
                  {status}
                </span>
              )}
            </div>
            {streaming && (
              <Button variant="outline" size="sm" onClick={stopJob}>
                <Square className="fill-current" />
                Stop
              </Button>
            )}
          </div>
          {streaming && (
            <p className="px-5 pt-1 text-xs text-muted-foreground">
              Running in the background — safe to leave this page.
            </p>
          )}
          <div className="mt-3 max-h-72 overflow-auto px-5">
            <ul className="space-y-1.5">
              {log.map((l, i) => (
                <li key={i} className="flex items-start gap-2 text-sm">
                  <StatusDot
                    tone={
                      l.tone === 'good'
                        ? 'verified'
                        : l.tone === 'bad'
                          ? 'critical'
                          : l.tone === 'warn'
                            ? 'minor'
                            : 'primary'
                    }
                    className={cn('mt-1.5 shrink-0', l.tone === 'info' && 'bg-muted-foreground/40')}
                  />
                  <span
                    className={cn(
                      'leading-relaxed',
                      l.tone === 'bad'
                        ? 'text-critical'
                        : l.tone === 'good'
                          ? 'text-foreground'
                          : 'text-muted-foreground',
                    )}
                  >
                    {l.message}
                  </span>
                </li>
              ))}
            </ul>
            <div ref={logEndRef} />
          </div>
        </Card>
      )}

      {/* Ingested documents */}
      <Card className="py-5">
        <div className="flex items-center justify-between px-5">
          <h2 className="text-base font-semibold">Ingested documents</h2>
          <span className="text-xs text-muted-foreground">
            {docs.length} document{docs.length === 1 ? '' : 's'}
          </span>
        </div>
        {docs.length === 0 ? (
          <p className="px-5 pt-3 text-sm text-muted-foreground">
            No documents ingested yet. Upload PDFs or ingest a folder above.
          </p>
        ) : (
          <div className="mt-3 divide-y divide-border/60">
            {docs.map((d) => (
              <div key={d.id} className="flex items-center gap-3 px-5 py-3">
                <FileText className="size-4 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">{d.filename}</p>
                  <p className="truncate text-xs text-muted-foreground">
                    {d.page_count} page{d.page_count === 1 ? '' : 's'}
                    {d.doc_type ? ` · ${d.doc_type.replace(/_/g, ' ')}` : ''}
                    {d.error ? ` · ${d.error.slice(0, 80)}` : ''}
                  </p>
                </div>
                <Badge variant={statusTone(d.status)}>{d.status}</Badge>
              </div>
            ))}
          </div>
        )}
      </Card>
    </>
  )
}
