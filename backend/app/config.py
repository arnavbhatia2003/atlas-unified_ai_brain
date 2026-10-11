"""Application configuration loaded from environment / .env.

Only the settings needed for scaffolding are declared here. Feature-specific
config (LLM, LangSmith, etc.) is read from the environment as those features
are built out.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres connection string (Supabase, via Supavisor pooler on :5432).
    database_url: str = Field(alias="DATABASE_URL")

    # Supabase gateway (Kong) URL — kept for later feature use.
    supabase_url: str = Field(default="http://localhost:8000", alias="SUPABASE_URL")

    # CORS: the frontend origin(s) allowed to call this API. In production set
    # FRONTEND_ORIGIN to your deployed frontend URL (e.g. the Vercel domain);
    # FRONTEND_ORIGINS may hold extra comma-separated origins. localhost is
    # always allowed via a regex for dev.
    frontend_origin: str = Field(
        default="http://localhost:5173", alias="FRONTEND_ORIGIN"
    )
    frontend_origins: str = Field(default="", alias="FRONTEND_ORIGINS")

    @property
    def allowed_origins(self) -> list[str]:
        out: list[str] = []
        for o in [self.frontend_origin, *self.frontend_origins.split(",")]:
            o = o.strip().rstrip("/")
            if o and o not in out:
                out.append(o)
        return out

    # Embeddings (NVIDIA NIM) for Phase-2 semantic matching.
    embeddings_enabled: bool = Field(default=True, alias="EMBEDDINGS_ENABLED")
    embedding_model: str = Field(
        default="nvidia/nemotron-3-embed-1b", alias="EMBEDDING_MODEL"
    )
    embedding_dim: int = Field(default=2048, alias="EMBEDDING_DIM")

    # NVIDIA NIM shared rate budget. The free tier caps at 40 requests/minute
    # across ALL calls (embeddings + chat LLM), so a single shared limiter sits
    # in front of every NVIDIA call. Default 35 leaves headroom below the cap so
    # minute-boundary bursts don't trip a 429.
    nvidia_max_rpm: int = Field(default=35, alias="NVIDIA_MAX_RPM")
    # Max burst allowed before the steady per-second refill throttles callers.
    nvidia_burst: int = Field(default=5, alias="NVIDIA_BURST")

    # Chat LLM (NVIDIA NIM via ChatNVIDIA) for chatbot + RCA/compliance workflows.
    # Primary reasoning model: nemotron-3-ultra-550b-a55b — a Mixture-of-Experts
    # model (~55B active of 550B total, 1M context) that streams first tokens in
    # ~1.9s despite its size, with strong agentic reasoning. All chat is streamed
    # (SSE) so perceived latency is time-to-first-token, not full completion.
    chat_model: str = Field(
        default="nvidia/nemotron-3-super-120b-a12b", alias="CHAT_MODEL"
    )
    # Optional lightweight model for high-frequency, low-stakes tasks (query
    # routing, quick classification) to conserve the shared rate budget.
    chat_fast_model: str = Field(
        default="nvidia/nemotron-3-super-120b-a12b", alias="CHAT_FAST_MODEL"
    )
    chat_temperature: float = Field(default=0.2, alias="CHAT_TEMPERATURE")

    # Cross-encoder reranker (NVIDIA NIM) for the final precision pass in hybrid
    # retrieval. One call per query; reorders the fused candidate pool.
    rerank_model: str = Field(
        default="nvidia/llama-nemotron-rerank-vl-1b-v2", alias="RERANK_MODEL"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
