"""Connectors — named data sources (IBM Maximo, SharePoint, an asset register…).

A connector either pulls records from an API endpoint or carries an inline JSON
payload (for when there's no live API to point at). Sync is:

  * incremental — the ingest pipeline only stores record_ids it hasn't seen, so
    re-syncing the same source adds nothing; adding new rows pulls only the delta.
  * accumulative — the pipeline re-resolves the whole corpus, so syncing one
    connector never wipes another.

Since we don't control source APIs, "new data only" is enforced on OUR side by
(system, record_id) dedupe — no assumption about the endpoint's own paging.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from app.db import get_pool

from . import pipeline


def _http_get_json(url: str) -> Any:
    import http.client
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Connector endpoint must be an HTTP(S) URL without credentials")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Connector endpoints must use public IP addresses")
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = cls(parsed.hostname, port=parsed.port, timeout=15)
    try:
        conn.connect()
        # Check the connected peer too: DNS can change between validation and connection.
        if not ipaddress.ip_address(conn.sock.getpeername()[0]).is_global:
            raise ValueError("Connector connected to a private IP address")
        target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        conn.request("GET", target, headers={"Accept":"application/json", "User-Agent":"UnifiedAssets/1.0"})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError(f"Connector returned HTTP {response.status}; redirects are not supported")
        data = response.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024:
            raise ValueError("Connector response exceeds 5 MB")
        return json.loads(data.decode())
    finally:
        conn.close()


def _coerce_records(data: Any) -> list[dict[str, Any]]:
    """Accept a list of records, {"records": [...]}, or a systems-map, or one record."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return []
    if data is None:
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("records"), list):
            return [d for d in data["records"] if isinstance(d, dict)]
        if data and all(isinstance(v, list) for v in data.values()):
            out: list[dict[str, Any]] = []
            for v in data.values():
                out.extend(d for d in v if isinstance(d, dict))
            return out
        return [data]
    return []


def _serialize(row: Any, record_count: int) -> dict[str, Any]:
    last_result = row["last_result"]
    if isinstance(last_result, str):
        last_result = json.loads(last_result)
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "kind": row["kind"],
        "endpoint": row["endpoint"],
        "status": row["status"],
        "records": record_count,
        "last_synced_at": row["last_synced_at"].isoformat() if row["last_synced_at"] else None,
        "last_result": last_result,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def create_connector(
    name: str,
    description: str,
    kind: str,
    endpoint: str | None = None,
    payload: Any = None,
) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into connectors (name, description, kind, endpoint, payload)
            values ($1,$2,$3,$4,$5::jsonb)
            returning *
            """,
            name, description, kind, endpoint,
            json.dumps(payload) if payload is not None else None,
        )
    return _serialize(row, 0)


async def list_connectors() -> list[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("select * from connectors order by created_at, id")
        counts = await conn.fetch(
            "select system, count(*) as c from source_records group by system"
        )
    by_system = {r["system"]: r["c"] for r in counts}
    return [_serialize(r, by_system.get(r["name"], 0)) for r in rows]


async def delete_connector(connector_id: int) -> AsyncIterator[dict[str, Any]]:
    """Delete a connector, drop its records, and re-resolve the remaining corpus."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("select name from connectors where id=$1", connector_id)
        if row is None:
            yield {"step": "error", "message": "Connector not found"}
            return
        await conn.execute("delete from source_records where system=$1", row["name"])
        await conn.execute("delete from connectors where id=$1", connector_id)
    yield {"step": "start", "message": f"Removed {row['name']}; re-resolving remaining sources"}
    async for ev in pipeline.ingest_records({}):
        if ev.get("step") == "start":
            continue
        yield ev


async def sync_connector(
    connector_id: int, override_payload: Any = None
) -> AsyncIterator[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        if override_payload is not None:
            await conn.execute(
                "update connectors set payload=$2::jsonb where id=$1",
                connector_id, json.dumps(override_payload),
            )
        row = await conn.fetchrow("select * from connectors where id=$1", connector_id)
    if row is None:
        yield {"step": "error", "message": "Connector not found"}
        return

    name = row["name"]
    yield {"step": "start", "message": f"Syncing {name}"}
    await _set_status(connector_id, "syncing")

    try:
        if row["kind"] == "api" and row["endpoint"]:
            data = await asyncio.to_thread(_http_get_json, row["endpoint"])
        else:
            data = row["payload"]
        records = _coerce_records(data)
    except Exception as exc:  # noqa: BLE001 - surface fetch failure to the stream
        await _set_status(connector_id, "error")
        yield {"step": "error", "message": f"Fetch failed: {exc}"}
        return

    yield {"step": "fetch", "message": f"Fetched {len(records)} record(s) from {name}"}

    summary: dict[str, Any] | None = None
    try:
        async for ev in pipeline.ingest_records({name: records}):
            if ev.get("step") == "start":
                continue
            if ev.get("step") == "complete":
                summary = ev.get("summary")
            yield ev
        await _finish(connector_id, summary)
    except BaseException:
        await _set_status(connector_id, "error")
        raise



async def _set_status(connector_id: int, status: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "update connectors set status=$2 where id=$1", connector_id, status
        )


async def _finish(connector_id: int, summary: dict[str, Any] | None) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "update connectors set status='idle', last_synced_at=now(), "
            "last_result=$2::jsonb where id=$1",
            connector_id, json.dumps(summary) if summary is not None else None,
        )
