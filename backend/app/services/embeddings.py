"""NVIDIA NIM embeddings for Phase-2 semantic matching.

Model: nvidia/nemotron-3-embed-1b (2048-dim, free hosted endpoint). Records are
embedded as passages (symmetric record-to-record comparison), and similarity is
cosine. If the embedding service is unavailable, resolution degrades gracefully
to exact-identifier (anchor) matching only.
"""

from __future__ import annotations

import math
import time
import warnings
from functools import lru_cache

from app.config import get_settings
from app.engine.resolution.extractors import SimilarityFn
from app.engine.resolution.models import SourceRecord

# Benign: the installed client's static model catalog predates this (new) model,
# so it warns "type unknown". Inference works correctly; silence the noise.
warnings.filterwarnings(
    "ignore", message=r".*type is unknown and inference may fail.*"
)

# Trace embedding calls in LangSmith when tracing is enabled (no-op otherwise).
try:
    from langsmith import traceable
except Exception:  # pragma: no cover - langsmith optional at runtime
    def traceable(*_args, **_kwargs):  # type: ignore[no-redef]
        def _wrap(fn):
            return fn
        return _wrap


class EmbeddingClient:
    """Lazy wrapper over NVIDIAEmbeddings with graceful failure."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._embedder = None
        self._retry_at = 0.0

    @property
    def available(self) -> bool:
        return time.monotonic() >= self._retry_at

    def _embedder_or_load(self):
        if self._embedder is None:
            from langchain_nvidia_ai_endpoints import NVIDIAEmbeddings

            self._embedder = NVIDIAEmbeddings(model=self.model)
        return self._embedder

    @traceable(run_type="embedding", name="nvidia-embed")
    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed passages. Returns vectors, or None if the service failed.

        All records are sent in ONE ``embed_documents`` call, so a batch of N
        records costs a single request against the shared NVIDIA rate budget.
        """
        if not self.available or not texts:
            return None
        try:
            # Draw one token from the shared NVIDIA budget (embeddings + chat)
            # before hitting the API, so we stay under the free-tier 40 rpm cap.
            from app.services.rate_limit import get_rate_limiter

            embedder = self._embedder_or_load()
            batch_size = getattr(embedder, "max_batch_size", 50)
            vectors = []
            for start in range(0, len(texts), batch_size):
                get_rate_limiter().acquire(blocking=True)
                vectors.extend(embedder.embed_documents(texts[start:start + batch_size]))
            return vectors
        except Exception:
            self._retry_at = time.monotonic() + 10
            return None

    @traceable(run_type="embedding", name="nvidia-embed-query")
    def embed_query(self, text: str) -> list[float] | None:
        """Embed a single query for retrieval (input_type=query, asymmetric).

        Used by the chatbot's grounded RAG path to search source_records. Returns
        the query vector, or None if the service is unavailable.
        """
        if not self.available or not text.strip():
            return None
        try:
            from app.services.rate_limit import get_rate_limiter

            get_rate_limiter().acquire(blocking=True)
            return self._embedder_or_load().embed_query(text)
        except Exception:
            self._retry_at = time.monotonic() + 10
            return None


@lru_cache
def get_embedding_client() -> EmbeddingClient:
    return EmbeddingClient(get_settings().embedding_model)


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a or not all(math.isfinite(x) for x in [*a, *b]):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def build_similarity(vectors_by_id: dict[str, list[float]]) -> SimilarityFn:
    """A resolution SimilarityFn backed by precomputed embedding vectors."""

    def similarity(a: SourceRecord, b: SourceRecord) -> float:
        va = vectors_by_id.get(a.record_id)
        vb = vectors_by_id.get(b.record_id)
        if not va or not vb:
            return 0.0
        return max(0.0, cosine(va, vb))

    return similarity
