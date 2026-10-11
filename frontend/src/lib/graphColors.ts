import type { NodeType } from './types'

/** Node dot colors by type (hex, since the canvas renderer needs literals). */
export const NODE_COLORS: Record<NodeType, string> = {
  asset: '#ff2a2a', // hazard red — canonical equipment
  record: '#2f9e7e', // green — source records / documents
  permit: '#2f8f9e', // (rules render teal)
  rule: '#2f8f9e', // teal — permits / rules
  signal: '#c2922a', // gold — sensors / signals
  failure: '#ff4a3d', // red — failure modes
  cause: '#c06a2b', // amber — root causes
  work_order: '#6b7280', // slate — work orders
  identifier: '#7a5cd0', // violet — identifiers
  concept: '#8a8f98', // gray — other
} as unknown as Record<NodeType, string>

export const NODE_TYPE_LABEL: Record<NodeType, string> = {
  asset: 'Asset',
  record: 'Record',
  failure: 'Failure',
  cause: 'Cause',
  rule: 'Rule / Permit',
  work_order: 'Work order',
  signal: 'Signal',
  identifier: 'Identifier',
  concept: 'Concept',
}

export function nodeColor(type: NodeType): string {
  return NODE_COLORS[type] ?? '#8a8f98'
}
