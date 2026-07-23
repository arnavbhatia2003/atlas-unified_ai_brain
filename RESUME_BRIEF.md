# Atlas — Resume Brief (Facts Only)

## Project name
**Atlas — Unified Asset Intelligence**

## Repository
https://github.com/arnavbhatia2003/atlas-unified_ai_brain

## Timeline
July 2026 – Present

## Context
Solo full-stack project built for a hackathon (results pending).

## Deployment status
Live deployment:
- Frontend: https://atlas-panda-psi.vercel.app (Vercel)
- Backend: https://atlas-api-pa3s.onrender.com (Render free tier, text-only PDF parsing)
- Database: Supabase hosted (Postgres + pgvector, project: kkyofhbiqgkjfgyrqzxv)

## What it does (one sentence)
Resolves the same physical asset across siloed operational systems (CMMS, incident logs, compliance registers, PDFs) into one canonical knowledge graph, then answers questions, diagnoses root causes, and checks compliance with every claim traced to a source.

## Core technical capabilities
1. **Entity resolution to canonical assets** — merges records from different sources (different identifiers, different schemas) into one unified asset using shared identifier validation + embedding similarity, with a confidence formula and human review queue for ambiguous matches.

2. **LangGraph-orchestrated workflows** — multi-step reasoning pipelines (root-cause analysis, compliance propagation, chat routing) coordinated via LangGraph, with LangSmith tracing for every LLM call.

3. **Hybrid retrieval (vector + full-text + rerank)** — Reciprocal Rank Fusion over dense embeddings (source records + OKF asset documents) and full-text search, reranked with NVIDIA Nemotron cross-encoder.

4. **Multi-hop root-cause analysis** — follows asset dependencies (DEPENDS_ON edges) to attribute downstream symptoms to upstream failures, calibrates confidence to evidence strength, refuses to guess when evidence is thin.

5. **Deterministic compliance checking** — at-risk propagation via graph traversal (recursive CTEs), not LLM guesses.

6. **Generalized ingestion** — structured JSON/API connectors + PDF parsing (via Docling locally; text-only on free deployment due to RAM constraints), with durable chain-of-custody (parse stored before extraction so source is never lost).

7. **Honest evaluation harness** — grades grounding (every hypothesis cites real evidence), calibration (confidence tracks evidence strength), resolution F1, anomaly gating, cross-asset leakage, and compliance determinism — not keyword matching.

## Tech stack (key choices)
- **One database:** PostgreSQL + pgvector (vector search + graph via recursive CTEs + identity resolution) — no Neo4j, no Qdrant
- **Backend:** Python 3.13, FastAPI (async), Server-Sent Events for live status streaming
- **LLM/embeddings/rerank:** NVIDIA NIM (Nemotron models) via langchain-nvidia-ai-endpoints
- **Orchestration:** LangChain + LangGraph for multi-step workflows
- **Document parsing:** Docling (PDF → structured with OCR, tables, layout) — tested locally; disabled on free deployment
- **Frontend:** Vite + React + TypeScript, Tailwind CSS, shadcn/ui

## Data scale (demo corpus)
- 3 unified canonical assets (1 manufacturing pump, 2 IT datacenter assets)
- 12 source records resolved across 5 different connector types
- 33 knowledge graph edges (15 operational)
- Cross-industry validation: manufacturing + IT in one corpus

## Key architectural decisions
1. **Single source of truth** — one Postgres database for identity, vectors, and graph (no secondary engines)
2. **Grounded-first design** — every answer cites source records; Open Knowledge Format (OKF) asset documents render all claims with citations
3. **Anomaly gating** — benign/within-tolerance logs filtered before diagnosis (LogSage pattern)
4. **Streaming wait UX** — real interim status over SSE, not silent spinners
5. **Confidence calibration** — high confidence only when resolving evidence exists; contradicting evidence shown, never dropped

## Distinguishing qualities
- Generalized (same pipeline handled manufacturing + IT; ingests arbitrary PDFs with inferred entity types)
- Honest evaluation (grounding + calibration metrics, not circular keyword tests)
- Production-ready deployment on free tier (Vercel + Supabase free, works within constraints)

## What NOT to say
- Do NOT claim "enterprise scale" or "production load" — this is a demo corpus
- Do NOT claim "full Docling deployment" — text-only on free tier
- Do NOT exaggerate with "AI-powered insights" or "cutting-edge" — describe what it does, not marketing fluff
- Do NOT say "machine learning models" unless referring to the specific NIM embeddings/LLM

## Resume entry tone
To-the-point technical description. A recruiter or hiring manager should understand:
- What problem it solves
- What technical depth it demonstrates (entity resolution, LangGraph orchestration, hybrid retrieval, multi-hop reasoning)
- That it's deployed and works

No filler words like "leveraged," "cutting-edge," "robust," "innovative." Use precise technical terms.
