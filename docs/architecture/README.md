# Atlas - architecture, layer by layer

These layer notes explain the earlier design. For current verified behavior, measured results and unresolved limits, read [project readiness](../project-readiness.md) and the [source-pinned interactive architecture](../atlas-architecture.html).

Read top to bottom. Each file is one idea, one diagram, minimal text. Start with the
whole system, then peel one layer at a time - the same way you'd explain it to
someone seeing it for the first time.

| # | Layer | The one thing it shows |
|---|-------|------------------------|
| 00 | [The problem](00-problem.md) | One asset, many systems, no shared key |
| 01 | [System overview](01-system-overview.md) | The whole thing in five blocks |
| 02 | [Ingestion](02-ingestion.md) | Store new, rebuild the whole picture |
| 03 | [Chain-of-custody](03-chain-of-custody.md) | A document is never silently lost |
| 04 | [Schema discovery](04-schema-discovery.md) | Three votes decide what a field is |
| 05 | [Resolution + confidence](05-resolution-confidence.md) | The merge, and its exact formula |
| 06 | [Knowledge graph](06-knowledge-graph.md) | Two layers of links in one table |
| 07 | [OKF](07-okf.md) | Each asset written up, every claim cited |
| 08 | [Retrieval](08-retrieval-rag.md) | Three searches, fused, re-scored |
| 09 | [Ask copilot](09-ask-copilot.md) | A router, not a free agent |
| 10 | [Root cause](10-rca.md) | Reason only when evidence exists |
| 11 | [Compliance](11-compliance.md) | The at-risk list is a query, not a guess |
| 12 | [Data model](12-data-model.md) | The core tables |
| 13 | [End-to-end](13-end-to-end.md) | Data in, answer out |

Every tool is introduced once, at the layer that uses it: Docling (03), pgvector and
full-text search (08, 12), Reciprocal Rank Fusion and reranking (08), LangGraph (09),
recursive queries (06), OKF (07), Server-Sent Events (13).

## The deliverable: one descriptive PDF
- **[atlas-report.md](atlas-report.md)** - the full descriptive document: the same
  onion layers as above, with all diagrams inline, plus worked examples, facts,
  tech-at-its-layer, a stack-at-a-glance table, and the hard-part decisions. This is
  the source for the PDF. No screenshots.
- **[manus-brief-pdf.md](manus-brief-pdf.md)** - paste this into Manus (with
  `atlas-report.md` attached) to render the polished PDF; Manus owns all design.

The numbered files `00`–`13` are the individual diagram sources (edit here; they are
mirrored inside `atlas-report.md`). The `images/` folder holds product screenshots
from an earlier plan and is **not used** by the PDF.

## Viewing
Diagrams render on GitHub, in VS Code with a Mermaid extension, or at
[mermaid.live](https://mermaid.live). Line breaks use `<br/>` so boxes render
cleanly everywhere.
