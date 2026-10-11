"""Background ingestion jobs.

Document ingestion is slow (Docling parsing + per-chunk LLM extraction). It must
NOT be tied to the browser's SSE request — if the user navigates away, the work
has to keep running. So an ingest runs as a server-side asyncio task that appends
its status events to an in-memory buffer; the SSE endpoint merely *tails* that
buffer and can be disconnected/reconnected freely.

In-memory (single-process) is sufficient here: the backend is one uvicorn
process, and a completed/failed job's log is retained so the UI can show the
outcome when the user returns. Jobs are pruned after a while to bound memory.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

# Keep finished jobs around this long so a returning user still sees the result.
_RETAIN_SECONDS = 60 * 30
_HEARTBEAT_SECONDS = 12.0

EventGen = Callable[[], "AsyncIterator[dict[str, Any]]"]


class Job:
    def __init__(self, job_id: str, kind: str, label: str) -> None:
        self.id = job_id
        self.kind = kind  # 'upload' | 'path'
        self.label = label
        self.events: list[dict[str, Any]] = []
        self.status = "running"  # running | done | error | cancelled
        self.started_at = time.time()
        self.finished_at: float | None = None
        self._new = asyncio.Event()
        self._task: asyncio.Task | None = None

    def add(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)
        self._new.set()

    def finish(self, status: str) -> None:
        self.status = status
        self.finished_at = time.time()
        self._new.set()

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "event_count": len(self.events),
            "last_message": self.events[-1].get("message") if self.events else "",
        }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def _prune(self) -> None:
        now = time.time()
        stale = [
            jid
            for jid, j in self._jobs.items()
            if j.status != "running"
            and j.finished_at
            and now - j.finished_at > _RETAIN_SECONDS
        ]
        for jid in stale:
            self._jobs.pop(jid, None)

    def start(self, kind: str, label: str, gen_factory: EventGen) -> Job:
        """Create a job and run `gen_factory()` as a detached background task."""
        self._prune()
        job = Job(uuid.uuid4().hex[:12], kind, label)
        self._jobs[job.id] = job

        async def runner() -> None:
            try:
                async for ev in gen_factory():
                    job.add(ev)
                job.finish("error" if any(e.get("step") in ("error", "doc_error") for e in job.events) else "done")
            except asyncio.CancelledError:
                job.add({"step": "cancelled", "message": "Ingestion cancelled"})
                job.finish("cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - surface, don't crash the server
                job.add({"step": "error", "message": f"Ingestion failed: {exc}"})
                job.finish("error")

        job._task = asyncio.create_task(runner())
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        self._prune()
        return [
            j.summary()
            for j in sorted(
                self._jobs.values(), key=lambda x: x.started_at, reverse=True
            )
        ]

    def running(self) -> Job | None:
        for j in sorted(self._jobs.values(), key=lambda x: x.started_at, reverse=True):
            if j.status == "running":
                return j
        return None

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job and job.status == "running" and job._task:
            job._task.cancel()
            return True
        return False

    async def stream(self, job: Job) -> AsyncIterator[dict[str, Any]]:
        """Yield the job's buffered events, then follow new ones until it ends.

        Reconnect-safe: a fresh subscription replays the whole buffer, so a user
        returning to the page sees the full log and live tail. The buffer is the
        source of truth, so no event is lost even across a heartbeat race.
        """
        cursor = 0
        while True:
            while cursor < len(job.events):
                yield job.events[cursor]
                cursor += 1
            if job.status != "running":
                yield {"step": "_end", "message": "", "status": job.status}
                return
            job._new.clear()
            try:
                await asyncio.wait_for(job._new.wait(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                # keep the SSE connection warm; the next loop re-scans the buffer
                yield {"step": "heartbeat", "message": ""}


# Process-wide singleton.
registry = JobRegistry()
