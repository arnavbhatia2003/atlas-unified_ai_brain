"""Atlas backend — FastAPI application entrypoint.

Scaffolding only: exposes a health-check endpoint that also confirms the
service can query Postgres (and that pgvector is available). No feature logic.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Use uvicorn's configured logger so startup status is visible in the server
# console (a bare custom logger has no handler under uvicorn).
logger = logging.getLogger("uvicorn.error")

# Load .env into os.environ so LangSmith tracing (LANGCHAIN_TRACING_V2, project,
# API key) and other env-based integrations are active in the server process.
# pydantic-settings reads .env for Settings, but does not populate os.environ.
load_dotenv()

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import router as api_router
from .config import get_settings
from .db import check_database, close_pool, init_pool


async def _warm_embeddings() -> None:
    """Pre-warm the NVIDIA NIM embedding model so the first real ingest isn't
    cold. The hosted endpoint scales to zero when idle; a cold request can take
    10-20s while warm requests are sub-second. Fire-and-forget: never blocks
    startup and never fails it if the service is unavailable.
    """
    try:
        from .services.embeddings import get_embedding_client

        client = get_embedding_client()
        result = await asyncio.to_thread(client.embed, ["warmup"])
        if result is not None:
            logger.info("Embedding model warmed (dim=%d)", len(result[0]))
        else:
            logger.warning("Embedding warm-up skipped: service unavailable")
    except Exception as exc:  # noqa: BLE001 - warm-up is best-effort
        logger.warning("Embedding warm-up failed: %s", exc)


async def _warm_docling() -> None:
    from .services.documents import warm_docling_worker
    try:
        await warm_docling_worker()
        logger.info("Docling OCR worker ready")
    except Exception as exc:
        logger.warning("Docling OCR warm-up unavailable (%s)", type(exc).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the Postgres pool on startup, tear it down on shutdown. If the DB
    # is unreachable, log the host + error clearly (a hung/blank startup is the
    # usual "stuck at Deploying" symptom) before failing — never leak the password.
    try:
        await init_pool()
    except Exception as exc:  # noqa: BLE001 - surface DB startup failures loudly
        from urllib.parse import urlsplit

        host = urlsplit(get_settings().database_url).hostname or "?"
        logger.error(
            "Database connection FAILED at startup (host=%s): %s. "
            "Check DATABASE_URL — for Supabase use the IPv4 'Session pooler' "
            "string (user postgres.<ref>), not the IPv6 'Direct connection'.",
            host,
            exc,
        )
        raise
    # Warm the embedding endpoint in the background (does not block readiness).
    warmup_task = asyncio.create_task(_warm_embeddings())
    docling_task = asyncio.create_task(_warm_docling())
    try:
        yield
    finally:
        warmup_task.cancel()
        docling_task.cancel()
        from .services.documents import close_docling_worker
        await close_docling_worker()
        await close_pool()


app = FastAPI(title="Atlas API", version="0.1.0", lifespan=lifespan)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    # Configured production origin(s), plus any localhost port for dev
    # (Vite may fall back from :5173 to :5174 if the port is taken).
    allow_origins=settings.allowed_origins,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# NOTE: this FastAPI build's include_router() fails to attach this prefixed
# router's routes (verified). The routes already carry their full "/api" paths,
# so attach them directly.
app.router.routes.extend(api_router.routes)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "atlas-api", "status": "ok"}


@app.get("/api/health")
async def health() -> JSONResponse:
    """Report service health, including a live Postgres query.

    Returns 200 when the database is reachable, 503 otherwise. The response
    always names what was checked so the frontend can show real status rather
    than a generic spinner outcome.
    """
    try:
        db = await check_database()
        return JSONResponse(
            status_code=200,
            content={
                "status": "healthy",
                "checks": {
                    "api": {"ok": True},
                    "database": {"ok": True, **db},
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - surface the failure explicitly
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "checks": {
                    "api": {"ok": True},
                    "database": {"ok": False, "error": str(exc)},
                },
            },
        )
