# Resume Entry — Atlas Project

Copy-paste this into your resume under the **Projects** section.

---

## Atlas — Unified Asset Intelligence
**July 2026 – Present** | [Live Demo](https://atlas-panda-psi.vercel.app) | [GitHub](https://github.com/arnavbhatia2003/atlas-unified_ai_brain)

Solo full-stack system that resolves the same physical asset across siloed operational sources (CMMS, incident logs, PDFs) into one canonical knowledge graph, then answers questions, diagnoses root causes, and checks compliance with every claim traced to a source record.

- Built entity resolution engine using shared identifier validation + embedding similarity with confidence scoring; human review queue surfaces ambiguous matches rather than auto-merging weak evidence
- Orchestrated multi-step reasoning workflows (root-cause analysis, compliance propagation, chat routing) via LangGraph with LangSmith tracing; multi-hop RCA follows asset dependencies (DEPENDS_ON edges) to attribute downstream symptoms to upstream failures
- Implemented hybrid retrieval: Reciprocal Rank Fusion over dense embeddings (source records + OKF asset documents) and full-text search, reranked with NVIDIA Nemotron cross-encoder
- Designed evaluation harness grading grounding (every hypothesis cites real evidence), confidence calibration, resolution F1, and anomaly gating — not keyword matching
- Deployed on Render (FastAPI backend with SSE streaming), Vercel (React + TypeScript), and Supabase (Postgres + pgvector for vectors + graph via recursive CTEs)

**Tech Stack:** Python 3.13 · FastAPI · LangChain/LangGraph · NVIDIA NIM (Nemotron) · Postgres + pgvector · React + TypeScript · Tailwind + shadcn/ui

---

## Alternative: Shorter Version (3 Bullets)

If you need a more compact version:

## Atlas — Unified Asset Intelligence
**July 2026 – Present** | [Live Demo](https://atlas-panda-psi.vercel.app) | [GitHub](https://github.com/arnavbhatia2003/atlas-unified_ai_brain)

- Built full-stack entity resolution system that merges operational records from siloed sources into canonical assets using identifier validation + embedding similarity, with confidence scoring and human review for weak matches; deployed on Vercel + Render + Supabase (Postgres + pgvector)
- Orchestrated LangGraph workflows for multi-hop root-cause analysis (follows asset dependencies to trace failures) and compliance checking (graph traversal via recursive CTEs); implemented hybrid retrieval (RRF over vector + full-text + cross-encoder rerank)
- Designed honest evaluation harness grading grounding (hypotheses cite real evidence), confidence calibration, and resolution F1 rather than keyword matching

**Tech:** Python · FastAPI · LangChain/LangGraph · NVIDIA NIM · Postgres + pgvector · React + TypeScript

---

## Notes

**What this entry demonstrates:**
- ✅ Systems thinking (entity resolution, knowledge graph, multi-hop reasoning)
- ✅ Real-world problem (siloed operational data)
- ✅ Modern ML stack (LangChain, LangGraph, embeddings, cross-encoder)
- ✅ Production deployment (live URLs, proper infra)
- ✅ Evaluation rigor (grounding, calibration metrics)

**What it avoids:**
- ❌ Filler words ("leveraged," "robust," "cutting-edge")
- ❌ Vague claims ("AI-powered insights")
- ❌ Exaggerated scale ("enterprise," "production load")

**For interviews:**
- Be ready to explain: What's entity resolution? Why confidence calibration matters? What's multi-hop RCA?
- Walk through: The demo at https://atlas-panda-psi.vercel.app
- Show code: Point to specific files (e.g., `backend/app/engine/resolution/`, `backend/app/workflows/rca.py`)

---

**File locations:**
- Full project facts: `RESUME_BRIEF.md`
- Deployment details: `FINAL_DEPLOYMENT_SUMMARY.md`
- Cleanup tasks: `CLEANUP_CHECKLIST.md`
