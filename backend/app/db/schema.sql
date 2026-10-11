-- Unified Assets Brain — schema (ONE database: Supabase Postgres + pgvector).
-- The knowledge graph is the `edges` table (traversed with recursive CTEs),
-- NOT a separate graph database. See Tech Stack steering + decision log.

create extension if not exists vector;

-- ---------------------------------------------------------------------------
-- Layer 1/2 — source systems and their raw records + discovered schema
-- ---------------------------------------------------------------------------
create table if not exists source_systems (
    id            bigserial primary key,
    name          text not null unique,          -- e.g. "SAP_PM", "ServiceNow"
    industry      text,                           -- detected industry
    connected_at  timestamptz not null default now()
);

create table if not exists source_records (
    id             bigserial primary key,
    system         text not null,
    record_id      text not null,                 -- native id within the system
    raw_json       jsonb not null,                -- untouched source payload
    semantic_text  text,                          -- text used for embedding match
    embedding      vector(2048),                  -- pgvector; nvidia/nemotron-3-embed-1b
    ingest_ts      timestamptz not null default now(),
    unique (system, record_id)
);
create index if not exists source_records_system_idx on source_records (system);

create table if not exists source_record_revisions (
    id bigserial primary key,
    source_record_id bigint not null references source_records(id) on delete cascade,
    raw_json jsonb not null,
    replaced_at timestamptz not null default now()
);

-- Result of schema discovery per (system, field), plus the human-validation lock.
create table if not exists discovered_fields (
    id                 bigserial primary key,
    system             text not null,
    field_name         text not null,
    detected_concept   text not null,
    role               text not null,             -- anchor|probable_anchor|descriptive|transactional|internal_id
    weight             real not null default 0,
    confidence         real not null default 0,
    cardinality_ratio  real,
    pattern_concept    text,
    semantic_concept   text,
    semantic_score     real,
    is_locked          boolean not null default false,  -- confirmed by a human
    locked_concept     text,
    locked_by          text,
    updated_at         timestamptz not null default now(),
    unique (system, field_name)
);

-- ---------------------------------------------------------------------------
-- Layer 3/4 — unified assets (canonical) + identifiers + graph projection
-- ---------------------------------------------------------------------------
create table if not exists unified_assets (
    unified_id     text primary key,              -- e.g. "ua-4521987"
    asset_name     text,
    entity_type    text not null default 'Asset',
    status         text not null default 'active',
    needs_review   boolean not null default false,
    review_reason  text not null default '',
    created_at     timestamptz not null default now()
);

-- Multi-valued identifiers (serial: typically 1; MACs/tags: N).
create table if not exists asset_identifiers (
    id             bigserial primary key,
    unified_id     text not null references unified_assets (unified_id) on delete cascade,
    concept        text not null,                 -- serial_number | mac_address | asset_tag | ...
    value          text not null,
    source_system  text,
    is_primary     boolean not null default false
);
create index if not exists asset_ident_value_idx on asset_identifiers (concept, lower(value));
create index if not exists asset_ident_asset_idx on asset_identifiers (unified_id);

-- Which raw source records were merged into a unified asset, and how.
create table if not exists asset_source_records (
    unified_id        text not null references unified_assets (unified_id) on delete cascade,
    source_record_id  bigint not null references source_records (id) on delete cascade,
    confidence        real not null,
    method            text not null,              -- anchor | embedding | manual
    primary key (unified_id, source_record_id)
);

-- Knowledge graph (a PROJECTION of the canonical model). Two layers.
create table if not exists edges (
    id            bigserial primary key,
    source_id     text not null,
    relation_type text not null,                  -- HAS_SERIAL, HAS_FAILURE, VIOLATES, ...
    target_id     text not null,
    layer         text not null check (layer in ('physical', 'operational')),
    metadata      jsonb not null default '{}'::jsonb
);
create index if not exists edges_source_idx on edges (source_id);
create index if not exists edges_target_idx on edges (target_id);
create index if not exists edges_layer_idx on edges (layer);

-- Anything a human must confirm: probable anchors, suggested merges, bridge
-- records with conflicting identifiers.
create table if not exists review_queue (
    id          bigserial primary key,
    kind        text not null,                    -- anchor_confirm | suggest_merge | bridge_conflict
    payload     jsonb not null,
    reason      text not null default '',
    status      text not null default 'open',     -- open | resolved | dismissed
    created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Connectors — named data sources (like IBM Maximo, SharePoint). Each connector
-- pulls records from an API endpoint OR carries an inline JSON payload. Sync is
-- incremental (only record_ids not already in source_records) and accumulative
-- (re-resolves over ALL stored source_records; never wipes other sources).
-- ---------------------------------------------------------------------------
create table if not exists connectors (
    id             bigserial primary key,
    name           text not null unique,           -- also the source_records.system
    description    text not null default '',
    kind           text not null default 'manual',  -- 'manual' (inline JSON) | 'api'
    endpoint       text,                            -- for kind='api'
    payload        jsonb,                           -- for kind='manual': list of records
    cursor         jsonb not null default '{}'::jsonb,
    status         text not null default 'idle',    -- idle | syncing | error
    last_synced_at timestamptz,
    last_result    jsonb,
    created_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- OKF bundle store — the "second brain". Each canonical asset is materialized
-- into an Open Knowledge Format concept document (human-readable markdown) that
-- is ALSO queryable by agents: `body` is embedded (pgvector) and full-text
-- indexed (tsv). Standalone (not FK'd to unified_assets, which is rebuilt every
-- ingest) so bundles persist; the materializer upserts current assets and prunes
-- orphans, re-embedding only when the body actually changed (body_hash).
-- ---------------------------------------------------------------------------
create table if not exists okf_documents (
    unified_id  text primary key,
    title       text,
    summary     text,                     -- one-line description for triage
    body        text not null default '', -- consolidated readable knowledge (searched)
    markdown    text not null default '', -- full OKF concept document
    body_hash   text,
    embedding   vector(2048),
    tsv         tsvector,
    updated_at  timestamptz not null default now()
);
create index if not exists okf_docs_tsv_idx on okf_documents using gin (tsv);

-- ---------------------------------------------------------------------------
-- Document parses (Docling) — the DURABLE chain-of-custody record for every
-- ingested PDF. The full structured parse (sections, tables, narrative chunks,
-- and Docling's raw export) is stored VERBATIM the moment parsing succeeds,
-- BEFORE any extraction/resolution runs. If post-parse processing fails, the
-- parse is recoverable from here and re-runnable — the source is never lost.
--   status: parsed  (durably saved, not yet turned into records)
--         | processed (records emitted + resolved into the corpus)
--         | error    (post-save processing failed; parse retained for retry)
-- Dedupe is by sha256 (re-uploading the identical file is a no-op).
-- ---------------------------------------------------------------------------
create table if not exists document_parses (
    id            bigserial primary key,
    system        text not null,                 -- connector name (one per doc)
    filename      text not null,
    sha256        text not null unique,          -- content hash (dedupe)
    doc_type      text,                          -- inferred document type (editable)
    parser        text,                          -- docling | docling-md
    page_count    int not null default 0,
    title         text,
    parsed        jsonb not null,                -- full ParsedDocument (durable)
    status        text not null default 'parsed',
    error         text,
    parsed_at     timestamptz not null default now(),
    processed_at  timestamptz
);
create index if not exists document_parses_system_idx on document_parses (system);
create index if not exists document_parses_status_idx on document_parses (status);

create table if not exists document_chunks (
    document_id bigint not null references document_parses(id) on delete cascade,
    chunk_index int not null,
    page int,
    section text not null default '',
    text text not null,
    tsv tsvector generated always as (to_tsvector('english', text) || to_tsvector('simple', text)) stored,
    primary key (document_id, chunk_index)
);
create index if not exists document_chunks_tsv_idx on document_chunks using gin(tsv);

-- ---------------------------------------------------------------------------
-- Document extraction cache — the generic information-extraction output for one
-- parsed PDF (asset/event "mention" records derived from tables + narrative
-- chunks). Cached by content hash so reprocessing a document never re-calls the
-- LLM (respects the NIM rate budget). See app/services/doc_extract.py.
-- ---------------------------------------------------------------------------
create table if not exists doc_extractions (
    sha256     text primary key,
    doc_type   text,
    records    jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Operational extraction cache — the structured event extracted from one
-- operational source record (work order / incident / inspection / rule). Cached
-- by record_id so re-resolution re-links edges to the current asset id WITHOUT
-- re-calling the LLM. See app/workflows/extract_ops.py.
-- ---------------------------------------------------------------------------
create table if not exists operational_extractions (
    record_id  text primary key,
    payload    jsonb not null,
    created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Workflow run history — every RCA / Compliance run is persisted so users can
-- reopen past analyses. The full grounded result is stored verbatim (jsonb); a
-- short summary + posture are denormalized for fast list rendering.
-- ---------------------------------------------------------------------------
create table if not exists workflow_runs (
    id          bigserial primary key,
    kind        text not null check (kind in ('rca', 'compliance')),
    question    text not null default '',
    asset       text,
    rule        text,
    summary     text not null default '',
    posture     text,                              -- compliance only
    resolved    boolean not null default false,
    result      jsonb not null,                    -- full grounded result payload
    created_at  timestamptz not null default now()
);
create index if not exists workflow_runs_kind_idx on workflow_runs (kind, created_at desc);

-- ---------------------------------------------------------------------------
-- Manual resolution decisions — a human's accept/keep-separate calls from the
-- review queue. Applied on every re-resolution so decisions are durable:
--   'merge'    -> force these two records into one asset
--   'separate' -> never merge these two records
-- ---------------------------------------------------------------------------
create table if not exists manual_decisions (
    id         bigserial primary key,
    kind       text not null check (kind in ('merge', 'separate')),
    record_a   text not null,
    record_b   text not null,
    note       text not null default '',
    created_at timestamptz not null default now(),
    unique (kind, record_a, record_b)
);
