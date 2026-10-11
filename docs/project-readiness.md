# Atlas: defensible scope and classroom acceptance

Atlas is a single-user prototype for reconciling operational asset records and answering questions with source evidence. It also supports document-only PDF questions: a general PDF need not contain assets and need not be forced into the asset graph. It is not an industrial digital twin, a safety decision system, or a replacement for Palantir, Cognite, IBM or SAP.

## Teacher demonstration

1. Start PostgreSQL, the backend on port 8001 and the frontend on port 5173 using the repository's setup instructions.
2. Open Workflows Ã¢â€ â€™ Documents and upload the PDF. Leave asset extraction unchecked for the fastest document question workflow.
3. Native text becomes searchable as soon as its durable save completes; image processing may still be running. Check the processing status and unreadable-page warning before treating coverage as complete. Click **Ask this PDF** to restrict retrieval to this document.
4. Ask a specific question, including the equipment identifier or terminology used in the PDF. Expand sources and open the original PDF at its cited page.
5. Ask a question whose answer is absent, and a question about conflicting values. Expect an explicit lack of evidence or both values, not a guessed reconciliation.
6. Enable asset extraction only for PDFs that actually describe assets and operational records. Inspect the review queue before treating proposed identities as confirmed.

Greetings and acknowledgments use a conversation reply rather than document search. Factual PDF responses show whether they are a model answer, source excerpts after an unavailable/unverified generation, or a no-matching-passages reply for which the model was not called. A no-source reply alone does not diagnose model connectivity.

## Why Docling is used selectively

Native PDFium extraction preserves selectable text quickly. Pages containing raster images are sent to a warmed, isolated Docling worker with RapidOCR/ONNX Runtime and layout/table parsing. Digital pages retain their original page numbers; OCR pages are mapped back to their original positions. The API saves the original PDF and native parse before OCR or asset enrichment. A worker failure returns a processing error and permits retry without losing that saved PDF.

The full `backend/requirements.txt` is required for OCR; `requirements-slim.txt` plus `DOCLING_ENABLED=false` supports selectable text only and rejects scans explicitly. Warm-up can download models on the first installation. A readiness log for the OCR worker matters; `/api/health` checks the API/database and does not certify OCR/model availability. English OCR is configured. Handwriting, poor scans, vector diagrams, equations and visual relationships are not reliably understood. A diagram's OCR labels do not establish its meaning. The default question path keeps digital tables as native text, which can lose column relationships. Selecting asset extraction also requests Docling layout/table parsing. This costs additional time.

## Functional requirements and evidence

| Requirement | Implemented behavior | Remaining limit |
|---|---|---|
| Answer from the uploaded PDF | Document scope, GIN-indexed passages, constrained model JSON, source handles and verbatim support checks | Up to 60,000 characters use whole-document context; larger PDFs retrieve 12 passages, with rare-term weighting. Long-document paraphrases and broad summaries can miss evidence |
| Handle mixed text/scans | Native extraction plus isolated Docling OCR; original page mapping | No universal handwriting/diagram interpretation |
| Preserve source evidence | Original bytes, hash, parsed passages, original-PDF page links | Bytes are bounded base64 JSON in PostgreSQL; object storage is preferable at larger scale |
| Admit absent/conflicting evidence | Prompted abstention/conflict disclosure, exact handles and supporting quotes checked against cited passages | Citations prove source existence, not semantic entailment; an LLM can still misread a passage |
| Extract assets when appropriate | Optional enrichment, literal source validation; equipment models/component types and pinout tables are excluded from installed-asset identity | Narrative extraction samples at most 24 passages; completeness is not guaranteed |
| Prevent unsafe identity bridges | Cluster-wide cannot-link, entity-kind and explicit namespace guards; singleton records without validated anchors require review | Proposed matches still share provisional clusters; IDs change when membership changes |
| Diagnose failures | Negation handling, citation checks, dependency traversal | RCA is a hypothesis, not proven causation; topology can be incomplete |
| Check compliance | Completion must match a rule and applicable supplied period | Missing periods, expiry, rule versions and legal interpretations require human judgment |
| Report failures honestly | Failed uploads/extraction mark errors; fallback answer modes are explicit; failed extraction is retryable | Jobs are process-local and do not survive server restart |
| Preserve revisions | Changed JSON records archive the old payload and invalidate stale embeddings/operational caches; PDF mention IDs include the content hash | This is source history, not temporal identity, replacement tracking or a full identity decision ledger |

## Non-functional requirements

| Requirement | Current bound | Evidence / gap |
|---|---|---|
| Upload resource control | 1Ã¢â‚¬â€œ5 PDFs, 25 MB each, 50 MB total, at most 1000 pages per PDF; one active upload job | Explicit validation; OCR has a five-minute processing timeout |
| Interactive latency | Fast native text; warmed OCR; no mandatory asset extraction for questions | See `atlas-pdf-acceptance.json` and `atlas-ocr-acceptance.json`; no production SLA |
| Recovery | Save before OCR/enrichment; embedding outage has retry cooldown | No durable distributed queue, resumable upload or whole-pipeline transaction |
| Concurrency | Single-process ingestion lock and shared model request budget | Not a multi-replica lock or load-tested service |
| Network boundaries | Public HTTP(S) connector endpoints, peer IP validation, size/time limits; project-confined path ingestion | No tenant authentication/authorization; run as a local demo, not a public private-data service |
| Privacy | Local original PDFs; optional tracing disabled by default | Retrieved passages go to the configured NVIDIA model; no certified data-residency policy |
| Confidence | Conservative resolution strength and retrieval scores | Scores are heuristics, not calibrated probabilities |
| Portability | Full OCR dependencies pinned; smaller text-only deployment option | OCR needs substantially more memory than a 512 MB free tier |

## What was corrected from the baseline audit

The interactive `atlas-assessment.html` and `atlas-audit-results.json` preserve revision `5491b78`, before remediation. The separately updated `atlas-architecture.html` explains the implemented current paths and links committed source evidence. Their counterexample scripts deliberately capture that baseline and should not be treated as tests that the fixed version must reproduce.

Remediation includes PDF-only retrieval and original-file custody, explicit upload limits, image OCR isolation, failed-job propagation, recoverable embeddings, passage-mode bundle embeddings, retrying absent embeddings, removing stale vectors when content changes, per-batch model budgeting, cluster-wide separation constraints, stable IDs for unchanged membership, preserving resolved review history, conservative mention resolution, operational negation, rule/period matching, exact RCA citations, connector network checks, and removal of unsupported landing-page accuracy claims. Regression tests cover these bounded behaviors. The earlier 42 findings are not all closed: the industrial design gaps in the tables above remain real.

## Evaluation without overstating accuracy

Run `backend/eval_document_qa.py` against the local API with valid NVIDIA credentials. It uploads fresh synthetic content, checks original-byte custody, retrieves facts near the beginning and end of a 150-page PDF, checks conflicting values and missing information, rejects a fake PDF, and tests an image-only middle page in a mixed PDF. The checked-in JSON records timings and pass/fail results. This is a small acceptance suite with known answers, repeated paragraphs and one local user, not an entity-resolution benchmark or a representative industrial PDF corpus.

For the teacher's actual document, label 15Ã¢â‚¬â€œ30 questions before testing: direct facts, tables, cross-page comparisons, absent information, contradictions and OCR-sensitive identifiers. Record answer correctness, source-page correctness, unsupported claims, abstention errors, unreadable pages, upload-to-ready time and answer latency. Keep retrieval misses separate from generation errors. Entity resolution requires a separate labeled same/different/ambiguous pair set and cluster-level evaluation; PDF Q&A cannot establish identity accuracy.


The earlier synthetic test was too narrow: answer words could appear in displayed fallback excerpts, so it did not establish model-generated answer quality. The new `backend/eval_manual_qa.py` requires `answer_mode=llm`, expected facts, actual used citations and expected physical PDF pages. Its report records failures before failing the run. It covers two official public PDFs: the 27-page Arduino UNO R3 manual and a 294-page Microchip ATmega328P datasheet. Cases include pinouts, capacities, voltage limits, conflicting clock specifications, late-page errata, missing values, document isolation and a conversational follow-up, plus four concurrent requests. Source excerpts never count as successful generated answers. See [the measured report](atlas-real-document-acceptance.json).

The wider evaluation exposed invalid model JSON and keyword ranking misses after unit tests passed. NVIDIA request parameters now disable Nemotron 3 hidden reasoning and constrain structured output. A subsequent correct summary used an unrelated citation; generation now supplies verbatim supporting quotes that are checked against the selected passages. These checks improve auditability but cannot prove semantic entailment or prevent every wrong conclusion.

The final recorded run passed all 18 checks. Selectable text became searchable in 0.444 seconds for the 27-page manual and 5.821 seconds for the 294-page datasheet. Full processing, including image-page OCR, took 55.673 and 22.989 seconds respectively. Sequential answer latency had a median of 2.290 seconds; four concurrent requests completed in 3.533 seconds. These are measurements on this laptop with the configured hosted provider, not guarantees for arbitrary scans or production traffic. Both stored originals matched the uploaded bytes.

Backend regression: 89 passed locally, including PostgreSQL revision and retrieval contracts. Frontend build passed; lint reports four existing warnings and no errors. CI now provisions a pgvector database instead of silently avoiding database contracts. Hosted-model and full OCR acceptance remain local checks because they need credentials and the full runtime.

## Document policy and source counts

| Input | Default treatment | Optional graph behavior |
|---|---|---|
| Manual, datasheet, reference paper | Preserve PDF and answer from its passages | Product models, component types, pin numbers and specifications are knowledge, not automatically installed units |
| Installed equipment register | Preserve PDF and enable asset extraction when requested | Identifier-bearing tables and sampled narrative mentions become source records; resolve validated identifiers, flag ambiguity |
| Incident, inspection or rule document | Preserve PDF; questions do not require a graph | Explicit asset/event mentions can create operational evidence; an event or mention does not itself prove identity |
| Mixed document | Use the same source-first question path | Narrative enrichment samples at most 24 eligible passages; unsampled sections remain searchable but may have no graph representation |
| Blank, protected or unreadable content | Surface processing errors or coverage warnings | Retain the original after durable save; reprocess when possible, do not manufacture facts |

PDF connectors show stored versions, searchable passages and separate asset mention counts. Zero asset mentions is valid for a reference manual or when extraction was not requested. Older caches report unknown extraction coverage. PDF connectors cannot perform JSON sync; reprocessing uses the stored original. Legacy uploads are backfilled into the passage index and PDF source list without changing their underlying records.

The entity-resolution research informs the distinctions above; it does not certify the implementation. Physical instances, functional positions, equipment models, measurements, observations and document revisions have different identity semantics. Explicit kinds and namespaces prevent contradictory merges when supplied. Unknown scope, replacements over time, aliases without strong evidence and provisional review clusters remain limitations. PDF answer tests are separate from a labeled same/different/ambiguous pair and cluster benchmark; no industrial accuracy percentage is justified here.

## Setup checked and what still needs evidence

Local PostgreSQL and real NVIDIA chat/embedding/reranking requests were exercised. Retired embedding/reranking endpoints were replaced, and ONNX Runtime was installed for OCR. Local secrets remain in ignored `.env` files. Other optional credentials were not rotated or certified; the app's required path is the direct PostgreSQL connection and NVIDIA key, with local Docling for OCR. LangSmith tracing is optional and disabled.

A six-month engineering project is defensible as a reproducible prototype with transparent limits, source-backed demonstrations and measured failure cases. Industrial deployment additionally requires authentication and tenant isolation, durable queues, temporal/versioned identity decisions, semantic retrieval for long documents, larger representative evaluation sets, load testing, observability, backup/restore validation and sector-specific review. Those requirements cannot be honestly satisfied by a small passing synthetic demonstration.
