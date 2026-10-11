"""NVIDIA NIM chat models for the chatbot + RCA/compliance workflows.

Every chat model shares the one process-wide rate budget (``get_rate_limiter``)
so chat and embeddings together stay under the free-tier 40 rpm cap. NIM also
throttles opportunistically ("traffic from other users may cause throttling"),
so ``resilient()`` wraps calls with bounded exponential-backoff retries to absorb
transient 429s instead of failing the request.

Two tiers (see config / Tech Stack decision log):
  - ``get_chat_model()``      -> nemotron-3-ultra-550b-a55b (deep reasoning; MoE,
                                 ~1.9s TTFT). Used for RCA/compliance/ask answers.
  - ``get_fast_chat_model()`` -> nano-9b-v2 (cheap, high-frequency: intent routing).

All chat is streamed via SSE at the API layer, so perceived latency is
time-to-first-token, not full completion.
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
        # Nemotron nano is a reasoning model — hidden reasoning consumes tokens
        # before the visible answer, so give enough headroom that the final JSON
        # answer isn't truncated.
        max_completion_tokens=max_tokens,
        # Share the single NVIDIA token bucket with embeddings so chat + embed
        # together never exceed the free-tier request budget.
        rate_limiter=get_rate_limiter(),
    )


@lru_cache
def get_chat_model() -> ChatNVIDIA:
    """Primary reasoning model (RCA, compliance, grounded answers)."""
    settings = get_settings()
    return _build(settings.chat_model, settings.chat_temperature)


@lru_cache
def get_fast_chat_model() -> ChatNVIDIA:
    """Reasoning-capable small model for RCA/compliance JSON + intent routing.
    nano-9b is a reasoning model; bound the token budget so a bounded amount of
    hidden reasoning still leaves room for the JSON answer without runaway latency."""
    settings = get_settings()
    return _build(settings.chat_fast_model, 0.0, max_tokens=2048)


@lru_cache
def get_extract_model() -> ChatNVIDIA:
    """Fast, NON-reasoning path for high-volume structured extraction. Callers
    prepend a 'detailed thinking off' directive; a small token budget keeps
    per-record latency low (reasoning traces are what make nano slow)."""
    settings = get_settings()
    return _build(settings.chat_fast_model, 0.0, max_tokens=640)


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
