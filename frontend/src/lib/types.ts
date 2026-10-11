/** Shared API response shapes (mirror the FastAPI backend on :8001). */

export interface Connector {
  id: number
  name: string
  description: string
  kind: 'manual' | 'api'
  endpoint: string | null
  status: string
  records: number
  last_synced_at: string | null
  last_result: {
    new?: number
    skipped?: number
    unified_assets?: number
    edges?: number
    review_items?: number
  } | null
  created_at: string | null
}

export interface Overview {
  review_open: number
  unified_assets: number
  assets_needing_review: number
  source_records: number
  edges_total: number
  edges_operational: number
  recent_evidence: RecentEvidence[]
}

export interface RecentEvidence {
  citation: string
  system: string
  text: string
  unified_id: string | null
  ingested_at: string | null
}

export type NodeType =
  | 'asset'
  | 'record'
  | 'failure'
  | 'cause'
  | 'rule'
  | 'work_order'
  | 'signal'
  | 'identifier'
  | 'concept'

export interface GraphNode {
  id: string
  type: NodeType
  label: string
  /** injected by react-force-graph at runtime */
  x?: number
  y?: number
}

export interface GraphLink {
  source: string | GraphNode
  target: string | GraphNode
  relation: string
  layer: 'physical' | 'operational'
}

export interface GraphData {
  nodes: GraphNode[]
  links: GraphLink[]
  stats: { records: number; relationships: number }
}

export interface AssetIdentifier {
  concept: string
  value: string
  is_primary: boolean
}

export interface AssetSummary {
  unified_id: string
  asset_name: string | null
  needs_review: boolean
  review_reason: string
  identifiers: AssetIdentifier[]
}

export interface ReviewCandidate {
  record_id: string
  system: string
  text: string
  unified_id: string | null
  asset_name: string | null
  fields: Record<string, unknown>
}

export interface ReviewItem {
  id: number
  kind: string
  payload: Record<string, unknown>
  reason: string
  status: string
  candidates?: ReviewCandidate[]
}

/** A durably-stored document parse (chain-of-custody record). */
export interface DocumentParse {
  unreadable_pages?: number[]
  id: number
  system: string
  filename: string
  doc_type: string | null
  parser: string | null
  page_count: number
  title: string | null
  status: string // parsed | processed | error
  error: string | null
  parsed_at: string | null
  processed_at: string | null
}

/** A single Server-Sent Event from a streamed workflow. */
export interface WorkflowEvent {
  step: string
  message?: string
  text?: string // token deltas (ask)
  result?: unknown
  [k: string]: unknown
}

// --- workflow result payloads (inside the final `complete` event) ----------

export interface Citation {
  id: string
  unified_id: string | null
  system: string
  similarity: number
}

export interface OverviewAsset {
  unified_id: string
  asset_name: string | null
  needs_review: boolean
}

export interface ChatResult {
  intent: 'ask' | 'asset_lookup' | 'rca' | 'compliance' | 'overview'
  answer?: string
  citations?: Citation[] | string[]
  confidence?: number
  contradictions?: string[]
  facts?: AssetFacts | null
  neighborhood?: unknown[]
  candidates?: unknown[]
  evidence_from?: string
  // rca/compliance results are spread in when intent is rca/compliance
  report?: RcaReport | null
  narrative?: ComplianceNarrative | null
  at_risk_assets?: AtRiskAsset[]
  resolved?: boolean
  // overview intent
  assets?: OverviewAsset[]
  counts?: { assets: number; records: number; review_open: number; sources: number }
}

export interface AssetFacts {
  unified_id: string
  asset_name: string | null
  needs_review: boolean
  review_reason: string
  identifiers: Record<string, string[]>
  source_records: string[]
}

export interface Hypothesis {
  cause: string
  explanation: string
  evidence: string[]
  confidence: number
}

export interface RcaReport {
  summary: string
  hypotheses: Hypothesis[]
  contradictions: string[]
  unresolved: string[]
}

export interface RcaResult {
  resolved: boolean
  asset?: { unified_id: string; facts: AssetFacts | null } | null
  report: RcaReport | null
  causal_chain?: GraphWalkRow[]
  evidence_from: string
  message?: string
  resolution?: unknown
}

export interface GraphWalkRow {
  depth: number
  source_id: string
  relation_type: string
  target_id: string
  metadata: Record<string, unknown>
}

export interface AtRiskAsset {
  asset: string
  rule: string
  reason: string
}

export interface ComplianceNarrative {
  summary: string
  posture: 'compliant' | 'at_risk' | 'unknown'
  contradictions: string[]
  unresolved: string[]
}

export interface ComplianceResult {
  scope: 'rule' | 'asset' | 'none'
  rule_id: string | null
  asset: string | null
  at_risk_assets: AtRiskAsset[]
  narrative: ComplianceNarrative | null
  evidence_from: string
}


// --- OKF asset concept document (GET /api/asset/{id}) ----------------------
export interface AssetAlias {
  concept: string
  value: string
  sources: string[]
}

export interface AssetSource {
  record_id: string
  system: string
  connector: string
  connector_description: string
  relation: string
  captured_at: string | null
  fields: Record<string, unknown>
  text: string
}

export interface RelatedAsset {
  unified_id: string
  asset_name: string | null
  kind: 'shared_identifier' | 'similar'
  via: string
  confidence: number
}

export interface AssetDoc {
  unified_id: string
  title: string
  type: string
  needs_review: boolean
  review_reason: string
  aliases: AssetAlias[]
  sources: AssetSource[]
  related_assets: RelatedAsset[]
  citations: string[]
  markdown: string
  error?: string
}

// --- workflow run history (GET /api/history) -------------------------------
export interface HistoryItem {
  id: number
  kind: 'rca' | 'compliance'
  question: string
  asset: string | null
  rule: string | null
  summary: string
  posture: string | null
  resolved: boolean
  created_at: string | null
}

export interface HistoryRun extends HistoryItem {
  result: RcaResult | ComplianceResult
}
