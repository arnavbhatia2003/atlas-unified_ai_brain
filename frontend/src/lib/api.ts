import type {
  AssetDoc,
  AssetSummary,
  Connector,
  DocumentParse,
  GraphData,
  HistoryItem,
  HistoryRun,
  Overview,
  ReviewItem,
  WorkflowEvent,
} from './types'

// Strip any trailing slash(es) so a VITE_API_URL like "https://host/" doesn't
// produce double-slash paths ("https://host//api/...") that 404.
export const API_URL = (import.meta.env.VITE_API_URL ?? 'http://localhost:8001').replace(
  /\/+$/,
  '',
)

async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    headers: { Accept: 'application/json' },
    signal,
  })
  if (!res.ok) throw new Error(`GET ${path} failed: ${res.status}`)
  return (await res.json()) as T
}

async function sendJSON<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`${method} ${path} failed: ${res.status}`)
  return (await res.json()) as T
}

export interface ConnectorCreate {
  name: string
  description: string
  kind: 'manual' | 'api'
  endpoint?: string | null
  payload?: unknown
}

export const api = {
  overview: (signal?: AbortSignal) => getJSON<Overview>('/api/overview', signal),
  graph: (asset?: string, signal?: AbortSignal) =>
    getJSON<GraphData>(
      `/api/graph${asset ? `?asset=${encodeURIComponent(asset)}` : ''}`,
      signal,
    ),
  assets: (signal?: AbortSignal) => getJSON<AssetSummary[]>('/api/assets', signal),
  review: (signal?: AbortSignal) => getJSON<ReviewItem[]>('/api/review', signal),
  connectors: (signal?: AbortSignal) => getJSON<Connector[]>('/api/connectors', signal),
  createConnector: (body: ConnectorCreate) =>
    sendJSON<Connector>('POST', '/api/connectors', body),
  deleteConnector: (id: number) =>
    sendJSON<{ ok: boolean }>('DELETE', `/api/connectors/${id}`),
  resolveReview: (id: number, action: 'merge' | 'separate' | 'dismiss') =>
    sendJSON<{ ok: boolean; status?: string }>(
      'POST',
      `/api/review/${id}/resolve`,
      { action },
    ),
  asset: (id: string, signal?: AbortSignal) =>
    getJSON<AssetDoc>(`/api/asset/${encodeURIComponent(id)}`, signal),
  documents: (signal?: AbortSignal) =>
    getJSON<DocumentParse[]>('/api/documents', signal),
  startIngestPath: (path: string) =>
    sendJSON<IngestJob>('POST', '/api/documents/ingest-path', { path }),
  documentJobs: (signal?: AbortSignal) =>
    getJSON<{ jobs: IngestJobSummary[]; running: boolean }>(
      '/api/documents/jobs',
      signal,
    ),
  cancelDocumentJob: (jobId: string) =>
    sendJSON<{ ok: boolean }>('POST', `/api/documents/jobs/${jobId}/cancel`),
  history: (kind?: 'rca' | 'compliance', signal?: AbortSignal) =>
    getJSON<HistoryItem[]>(`/api/history${kind ? `?kind=${kind}` : ''}`, signal),
  historyRun: (id: number, signal?: AbortSignal) =>
    getJSON<HistoryRun>(`/api/history/${id}`, signal),
}

/** Parse an SSE `data: {json}\n\n` response body into WorkflowEvents. */
async function* readSSE(res: Response, label: string): AsyncGenerator<WorkflowEvent> {
  if (!res.ok || !res.body) {
    throw new Error(`${label} failed: ${res.status}`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let sep: number
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        const dataLine = frame.split('\n').find((l) => l.startsWith('data:'))
        if (!dataLine) continue
        const json = dataLine.slice(5).trim()
        if (json) {
          try {
            yield JSON.parse(json) as WorkflowEvent
          } catch {
            // ignore malformed frame
          }
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

/**
 * POST a JSON body and stream Server-Sent Events back as they arrive.
 * Abort via the AbortSignal (used to interrupt a running answer/workflow).
 */
export async function* streamWorkflow(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<WorkflowEvent> {
  const res = await fetch(`${API_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  })
  yield* readSSE(res, `POST ${path}`)
}

export interface IngestJob {
  job_id?: string
  status?: string
  label?: string
  count?: number
  error?: string
}

export interface IngestJobSummary {
  id: string
  kind: string
  label: string
  status: string // running | done | error | cancelled
  started_at: number
  finished_at: number | null
  event_count: number
  last_message: string
}

/** Upload PDFs (multipart) and START a background ingest job. Returns the job id
 *  immediately; the job keeps running server-side even if the page is left. */
export async function startDocumentUpload(files: File[], extractAssets = false): Promise<IngestJob> {
  const fd = new FormData()
  for (const f of files) fd.append('files', f)
  // Do NOT set Content-Type — the browser adds the multipart boundary.
  const res = await fetch(`${API_URL}/api/documents/ingest?extract_assets=${extractAssets}`, {
    method: 'POST',
    headers: { Accept: 'application/json' },
    body: fd,
  })
  if (!res.ok) throw new Error(`upload failed: ${res.status}`)
  return (await res.json()) as IngestJob
}

/** Tail a background ingest job's status (GET SSE). Reconnect-safe: replays the
 *  buffered log then follows the live tail. */
export async function* streamJob(
  jobId: string,
  signal?: AbortSignal,
): AsyncGenerator<WorkflowEvent> {
  const res = await fetch(`${API_URL}/api/documents/jobs/${jobId}/stream`, {
    headers: { Accept: 'text/event-stream' },
    signal,
  })
  yield* readSSE(res, `GET /api/documents/jobs/${jobId}/stream`)
}
