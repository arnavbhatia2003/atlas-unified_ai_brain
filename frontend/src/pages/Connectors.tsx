import { useMemo, useRef, useState } from 'react'
import {
  Check,
  Database,
  FileJson,
  Globe,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react'

import { PageHeader } from '@/components/PageHeader'
import { Badge, StatusDot } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Modal } from '@/components/ui/modal'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { api, streamWorkflow, type ConnectorCreate } from '@/lib/api'
import { useAsync } from '@/lib/useAsync'
import { cn } from '@/lib/utils'
import type { Connector, WorkflowEvent } from '@/lib/types'

export function Connectors() {
  const { data, loading, error, reload } = useAsync<Connector[]>((s) => api.connectors(s))
  const [addOpen, setAddOpen] = useState(false)
  const [syncId, setSyncId] = useState<number | null>(null)
  const [syncLines, setSyncLines] = useState<string[]>([])
  const [busyDelete, setBusyDelete] = useState<number | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  async function sync(c: Connector) {
    if (syncId) return
    setSyncId(c.id)
    setSyncLines([])
    const controller = new AbortController()
    abortRef.current = controller
    try {
      for await (const ev of streamWorkflow(
        `/api/connectors/${c.id}/sync`,
        { payload: null },
        controller.signal,
      ) as AsyncGenerator<WorkflowEvent>) {
        if (ev.message) setSyncLines((l) => [...l, `${ev.step}: ${ev.message}`])
      }
      reload()
    } catch {
      setSyncLines((l) => [...l, 'error: sync failed'])
    } finally {
      setSyncId(null)
      abortRef.current = null
    }
  }

  async function remove(c: Connector) {
    setBusyDelete(c.id)
    try {
      await api.deleteConnector(c.id)
      reload()
    } finally {
      setBusyDelete(null)
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="Incremental · accumulative"
        title="Connectors"
        description="Connect a source once. Each sync pulls only new records and merges them into the shared knowledge graph — existing sources are never overwritten."
        action={
          <Button onClick={() => setAddOpen(true)}>
            <Plus />
            Add connector
          </Button>
        }
      />

      {loading ? (
        <Card className="py-5">
          <div className="space-y-3 px-5">
            <Skeleton className="h-6 w-1/3" />
            <Skeleton className="h-6 w-1/2" />
          </div>
        </Card>
      ) : error ? (
        <Card className="py-5">
          <p className="px-5 text-sm text-critical">
            Couldn't load connectors. Is the backend running?
          </p>
        </Card>
      ) : !data || data.length === 0 ? (
        <Card className="py-12">
          <div className="flex flex-col items-center gap-2 px-5 text-center">
            <span className="flex size-11 items-center justify-center bg-secondary text-muted-foreground">
              <Database className="size-5" />
            </span>
            <p className="text-sm font-medium">No sources connected</p>
            <p className="max-w-xs text-sm text-muted-foreground">
              Add a connector — an API endpoint or inline JSON — to start building
              the knowledge graph.
            </p>
            <Button size="sm" className="mt-1" onClick={() => setAddOpen(true)}>
              <Plus />
              Add connector
            </Button>
          </div>
        </Card>
      ) : (
        <div className="flex flex-col gap-4">
          {data.map((c) => (
            <ConnectorCard
              key={c.id}
              connector={c}
              syncing={syncId === c.id}
              syncLines={syncId === c.id ? syncLines : []}
              deleting={busyDelete === c.id}
              disabled={syncId !== null && syncId !== c.id}
              onSync={() => sync(c)}
              onDelete={() => remove(c)}
            />
          ))}
        </div>
      )}

      <AddConnectorModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onCreated={() => {
          setAddOpen(false)
          reload()
        }}
      />
    </>
  )
}

function ConnectorCard({
  connector: c,
  syncing,
  syncLines,
  deleting,
  disabled,
  onSync,
  onDelete,
}: {
  connector: Connector
  syncing: boolean
  syncLines: string[]
  deleting: boolean
  disabled: boolean
  onSync: () => void
  onDelete: () => void
}) {
  const Icon = c.kind === 'api' ? Globe : FileJson
  return (
    <Card className="gap-0 py-5">
      <div className="flex flex-col gap-4 px-6 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex items-start gap-3">
          <span className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-secondary text-muted-foreground">
            <Icon className="size-5" />
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="text-base font-semibold">{c.name}</h2>
              <Badge variant={c.kind === 'api' ? 'linked' : 'neutral'}>
                {c.kind === 'api' ? 'API' : 'JSON'}
              </Badge>
            </div>
            {c.description && (
              <p className="mt-0.5 text-sm text-muted-foreground">{c.description}</p>
            )}
            <p className="mt-1 truncate font-mono text-xs text-muted-foreground">
              {c.endpoint ?? 'inline JSON payload'}
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span className="flex items-center gap-1.5">
                <StatusDot tone={c.records > 0 ? 'verified' : 'primary'} />
                {c.records} record{c.records === 1 ? '' : 's'}
              </span>
              <span>
                {c.last_synced_at
                  ? `Last synced ${new Date(c.last_synced_at).toLocaleString()}`
                  : 'Never synced'}
              </span>
              {c.last_result && (c.last_result.new ?? 0) > 0 && (
                <span>+{c.last_result.new} new last sync</span>
              )}
            </div>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="outline" size="sm" onClick={onSync} disabled={syncing || disabled}>
            {syncing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            {syncing ? 'Syncing…' : 'Sync'}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Delete ${c.name}`}
            onClick={onDelete}
            disabled={deleting || syncing}
          >
            {deleting ? <Loader2 className="animate-spin" /> : <Trash2 />}
          </Button>
        </div>
      </div>

      {syncing && syncLines.length > 0 && (
        <div className="mx-6 mt-4 space-y-1 rounded-xl bg-secondary/60 p-3">
          {syncLines.map((l, i) => (
            <p
              key={i}
              className={cn(
                'flex items-center gap-2 text-xs',
                i === syncLines.length - 1 ? 'text-foreground' : 'text-muted-foreground',
              )}
            >
              {i === syncLines.length - 1 ? (
                <Loader2 className="size-3 animate-spin" />
              ) : (
                <Check className="size-3 text-verified" />
              )}
              {l}
            </p>
          ))}
        </div>
      )}
    </Card>
  )
}

function AddConnectorModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean
  onClose: () => void
  onCreated: () => void
}) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [kind, setKind] = useState<'manual' | 'api'>('manual')
  const [endpoint, setEndpoint] = useState('')
  const [json, setJson] = useState(SAMPLE_JSON)
  const [submitting, setSubmitting] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const jsonError = useMemo(() => {
    if (kind !== 'manual') return null
    try {
      JSON.parse(json)
      return null
    } catch {
      return 'Not valid JSON.'
    }
  }, [json, kind])

  function reset() {
    setName('')
    setDescription('')
    setKind('manual')
    setEndpoint('')
    setJson(SAMPLE_JSON)
    setErr(null)
  }

  async function submit() {
    if (!name.trim() || submitting) return
    if (kind === 'manual' && jsonError) return
    if (kind === 'api' && !endpoint.trim()) return
    setSubmitting(true)
    setErr(null)
    try {
      const body: ConnectorCreate = {
        name: name.trim(),
        description: description.trim(),
        kind,
        endpoint: kind === 'api' ? endpoint.trim() : null,
        payload: kind === 'manual' ? JSON.parse(json) : null,
      }
      await api.createConnector(body)
      reset()
      onCreated()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Failed to add connector')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Add connector"
      description="Name a source, then point it at an API endpoint or paste its records as JSON."
    >
      <div className="space-y-4">
        <label className="block text-sm">
          <span className="mb-1.5 block font-medium">Name</span>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. IBM Maximo, SharePoint, Asset register"
          />
        </label>
        <label className="block text-sm">
          <span className="mb-1.5 block font-medium">Description</span>
          <Input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this source contains"
          />
        </label>

        <div className="flex gap-2 rounded-lg bg-secondary p-1">
          {(['manual', 'api'] as const).map((k) => (
            <button
              key={k}
              onClick={() => setKind(k)}
              className={cn(
                'flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
                kind === k
                  ? 'bg-card text-foreground shadow-soft'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {k === 'manual' ? 'Paste JSON' : 'API endpoint'}
            </button>
          ))}
        </div>

        {kind === 'api' ? (
          <label className="block text-sm">
            <span className="mb-1.5 block font-medium">Endpoint URL</span>
            <Input
              value={endpoint}
              onChange={(e) => setEndpoint(e.target.value)}
              placeholder="https://…/records.json"
            />
            <span className="mt-1.5 block text-xs text-muted-foreground">
              Must return a JSON array of records, or {'{'}"records": [...]{'}'}.
            </span>
          </label>
        ) : (
          <label className="block text-sm">
            <span className="mb-1.5 block font-medium">Records (JSON)</span>
            <Textarea
              value={json}
              onChange={(e) => setJson(e.target.value)}
              spellCheck={false}
              className="min-h-40 font-mono text-xs"
            />
            {jsonError && <span className="mt-1.5 block text-xs text-critical">{jsonError}</span>}
          </label>
        )}

        {err && <p className="text-sm text-critical">{err}</p>}

        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button
            onClick={submit}
            disabled={
              submitting ||
              !name.trim() ||
              (kind === 'manual' && !!jsonError) ||
              (kind === 'api' && !endpoint.trim())
            }
          >
            {submitting ? <Loader2 className="animate-spin" /> : <Plus />}
            Add connector
          </Button>
        </div>
      </div>
    </Modal>
  )
}

const SAMPLE_JSON = JSON.stringify(
  [
    { record_id: 'r1', serial_number: 'SN-100', description: 'Feed pump, plant 7' },
    { record_id: 'r2', serial_number: 'SN-101', description: 'Coolant valve, plant 7' },
  ],
  null,
  2,
)
