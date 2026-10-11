"""NVIDIA NIM chat models for the chatbot + RCA/compliance workflows.

Every chat model shares the one process-wide rate budget (``get_rate_limiter``)
so chat and embeddings together stay under the free-tier 40 rpm cap. NIM also
throttles opportunistically ("traffic from other users may cause throttling"),
so ``resilient()`` wraps calls with bounded exponential-backoff retries to absorb
transient 429s instead of failing the request.

Model names come from Settings. Structured answers are validated before their
text is emitted; SSE also carries retrieval and processing progress.
"""

from __future__ import annotations

from functools import lru_cache

from langchain_core.runnables import Runnable
from langchain_nvidia_ai_endpoints import ChatNVIDIA, NVIDIARerank

from app.config import get_settings
from app.services.rate_limit import get_rate_limiter


def _build(model: str, temperature: float, max_tokens: int = 1024) -> ChatNVIDIA:
    return ChatNVIDIA(
        model=model,
        temperature=temperature,
        max_completion_tokens=max_tokens,
        # Share the single NVIDIA token bucket with embeddings so chat + embed
        # together never exceed the free-tier request budget.
        rate_limiter=get_rate_limiter(),
        # Nemotron 3 uses a request parameter; the older textual prefix does not disable reasoning.
        model_kwargs={"chat_template_kwargs": {"enable_thinking": False}} if "nemotron-3-" in model else {},
    )


@lru_cache
def get_chat_model() -> ChatNVIDIA:
    """Primary reasoning model (RCA, compliance, grounded answers)."""
    settings = get_settings()
    return _build(settings.chat_model, settings.chat_temperature)


@lru_cache
def get_fast_chat_model() -> ChatNVIDIA:
    """Configured low-temperature model for grounded JSON and intent routing."""
    settings = get_settings()
    return _build(settings.chat_fast_model, 0.0, max_tokens=2048)


@lru_cache
def get_extract_model() -> ChatNVIDIA:
    """Bounded non-reasoning path for structured extraction."""
    settings = get_settings()
    return _build(settings.chat_fast_model, 0.0, max_tokens=640)


def json_response_format(name: str, schema: dict) -> dict:
    """Enforce JSON at the hosted endpoint, rather than only in prompt text."""
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}


def get_reranker(top_n: int = 8) -> NVIDIARerank:
    """Cross-encoder reranker (NIM) for the precision pass in hybrid retrieval.
    One request scores the whole candidate pool against the query, so it's a
    single call regardless of pool size. Construction is network-free."""
    settings = get_settings()
    return NVIDIARerank(model=settings.rerank_model, top_n=top_n)


def resilient(runnable: Runnable, attempts: int = 4) -> Runnable:
    """Wrap a model/chain with bounded backoff retries for transient throttling.

    NIM can 429 even below the nominal rate cap when the shared endpoint is busy.
    The shared limiter minimizes this; ``resilient`` handles the residual case so
    a transient throttle degrades to a short wait rather than a failed request.
    Retries preserve streaming: the wrapped runnable is still ``.stream``-able.
    """
    return runnable.with_retry(
        stop_after_attempt=attempts,
        wait_exponential_jitter=True,
    )
