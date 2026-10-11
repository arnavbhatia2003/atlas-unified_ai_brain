# Atlas: defensible scope and classroom acceptance

Atlas is a single-user prototype for reconciling operational asset records and answering questions with source evidence. It also supports document-only PDF questions: a general PDF need not contain assets and need not be forced into the asset graph. It is not an industrial digital twin, a safety decision system, or a replacement for Palantir, Cognite, IBM or SAP.

## Teacher demonstration

1. Start PostgreSQL, the backend on port 8001 and the frontend on port 5173 using the repository's setup instructions.
2. Open Workflows â†’ Documents and upload the PDF. Leave asset extraction unchecked for the fastest document question workflow.
3. Wait for processing to finish. Check any unreadable-page warning. Click **Ask this PDF** to restrict retrieval to this document.
4. Ask a specific question, including the equipment identifier or terminology used in the PDF. Expand sources and open the original PDF at its cited page.
5. Ask a question whose answer is absent, and a question about conflicting values. Expect an explicit lack of evidence or both values, not a guessed reconciliation.
6. Enable asset extraction only for PDFs that actually describe assets and operational records. Inspect the review queue before treating proposed identities as confirmed.

## Why Docling is used selectively

Native PDFium extraction preserves selectable text quickly. Pages containing raster images are sent to a warmed, isolated Docling worker with RapidOCR/ONNX Runtime and layout/table parsing. Digital pages retain their original page numbers; OCR pages are mapped back to their original positions. The API saves the original PDF and native parse before OCR or asset enrichment. A worker failure returns a processing error and permits retry without losing that saved PDF.

The full `backend/requirements.txt` is required for OCR; `requirements-slim.txt` plus `DOCLING_ENABLED=false` supports selectable text only and rejects scans explicitly. Warm-up can download models on the first installation. A readiness log for the OCR worker matters; `/health` checks the API/database and does not certify OCR/model availability. English OCR is configured. Handwriting, poor scans, vector diagrams, equations and visual relationships are not reliably understood. A diagram's OCR labels do not establish its meaning. Digital tables without images remain native text and may lose column relationships.

## Functional requirements and evidence

| Requirement | Implemented behavior | Remaining limit |
|---|---|---|
| Answer from the uploaded PDF | Document scope, page-level retrieval, validated source handles | Lexical retrieval can miss paraphrases; eight retrieved passages are not whole-document understanding |
| Handle mixed text/scans | Native extraction plus isolated Docling OCR; original page mapping | No universal handwriting/diagram interpretation |
| Preserve source evidence | Original bytes, hash, parsed passages, original-PDF page links | Bytes are bounded base64 JSON in PostgreSQL; object storage is preferable at larger scale |
| Admit absent/conflicting evidence | Prompted abstention/conflict disclosure, exact handle validation | Citations prove source existence, not semantic entailment; an LLM can still misread a passage |
| Extract assets when appropriate | Optional enrichment, literal identifier validation, deterministic discovery | Narrative extraction samples at most 24 passages; completeness is not guaranteed |
| Prevent unsafe identity bridges | Cluster-wide cannot-link checks; probable identifiers cannot anchor automatic merges | Proposed matches still share provisional clusters; IDs change when membership changes |
| Diagnose failures | Negation handling, citation checks, dependency traversal | RCA is a hypothesis, not proven causation; topology can be incomplete |
| Check compliance | Completion must match a rule and applicable supplied period | Missing periods, expiry, rule versions and legal interpretations require human judgment |
| Report failures honestly | Failed uploads mark jobs as errors; enrichment reports partial completion | Jobs are process-local and do not survive server restart |

## Non-functional requirements

| Requirement | Current bound | Evidence / gap |
|---|---|---|
| Upload resource control | 1â€“5 PDFs, 25 MB each, 50 MB total, at most 1000 pages per PDF; one active upload job | Explicit validation; OCR has a five-minute processing timeout |
| Interactive latency | Fast native text; warmed OCR; no mandatory asset extraction for questions | See `atlas-pdf-acceptance.json` and `atlas-ocr-acceptance.json`; no production SLA |
| Recovery | Save before OCR/enrichment; embedding outage has retry cooldown | No durable distributed queue, resumable upload or whole-pipeline transaction |
| Concurrency | Single-process ingestion lock and shared model request budget | Not a multi-replica lock or load-tested service |
| Network boundaries | Public HTTP(S) connector endpoints, peer IP validation, size/time limits; project-confined path ingestion | No tenant authentication/authorization; run as a local demo, not a public private-data service |
| Privacy | Local original PDFs; optional tracing disabled by default | Retrieved passages go to the configured NVIDIA model; no certified data-residency policy |
| Confidence | Conservative resolution strength and retrieval scores | Scores are heuristics, not calibrated probabilities |
| Portability | Full OCR dependencies pinned; smaller text-only deployment option | OCR needs substantially more memory than a 512 MB free tier |

## What was corrected from the baseline audit

The interactive `atlas-assessment.html`, architecture diagram and `atlas-audit-results.json` describe revision `5491b78`, before remediation. Their counterexample scripts deliberately capture that baseline and should not be treated as tests that the fixed version must reproduce.

Remediation includes PDF-only retrieval and original-file custody, explicit upload limits, image OCR isolation, failed-job propagation, recoverable embeddings, passage-mode bundle embeddings, retrying absent embeddings, removing stale vectors when content changes, per-batch model budgeting, cluster-wide separation constraints, stable IDs for unchanged membership, preserving resolved review history, conservative mention resolution, operational negation, rule/period matching, exact RCA citations, connector network checks, and removal of unsupported landing-page accuracy claims. Regression tests cover these bounded behaviors. The earlier 42 findings are not all closed: the industrial design gaps in the tables above remain real.

## Evaluation without overstating accuracy

Run `backend/eval_document_qa.py` against the local API with valid NVIDIA credentials. It uploads fresh synthetic content, checks original-byte custody, retrieves facts near the beginning and end of a 150-page PDF, checks conflicting values and missing information, rejects a fake PDF, and tests an image-only middle page in a mixed PDF. The checked-in JSON records timings and pass/fail results. This is a small acceptance suite with known answers, repeated paragraphs and one local user, not an entity-resolution benchmark or a representative industrial PDF corpus.

For the teacher's actual document, label 15â€“30 questions before testing: direct facts, tables, cross-page comparisons, absent information, contradictions and OCR-sensitive identifiers. Record answer correctness, source-page correctness, unsupported claims, abstention errors, unreadable pages, upload-to-ready time and answer latency. Keep retrieval misses separate from generation errors. Entity resolution requires a separate labeled same/different/ambiguous pair set and cluster-level evaluation; PDF Q&A cannot establish identity accuracy.


Latest local acceptance: 2.034 s to ready for the fresh 150-page digital PDF; 1.823–3.032 s for its five questions; 17.287 s for the mixed three-page text/scan PDF. All checked cases passed. Backend regression: 59 passed. Frontend build passed; lint has four existing warnings and no errors.

## Setup checked and what still needs evidence

Local PostgreSQL and real NVIDIA chat/embedding/reranking requests were exercised. Retired embedding/reranking endpoints were replaced, and ONNX Runtime was installed for OCR. Local secrets remain in ignored `.env` files. Other optional credentials were not rotated or certified; the app's required path is the direct PostgreSQL connection and NVIDIA key, with local Docling for OCR. LangSmith tracing is optional and disabled.

A six-month engineering project is defensible as a reproducible prototype with transparent limits, source-backed demonstrations and measured failure cases. Industrial deployment additionally requires authentication and tenant isolation, durable queues, versioned identity decisions, indexed document chunks, larger representative evaluation sets, load testing, observability, backup/restore validation and sector-specific review. Those requirements cannot be honestly satisfied by a small passing synthetic demonstration.
