"""Resolve a free-text asset mention to a canonical unified asset.

This reuses the canonicalization principle from ingestion: an incoming reference
must resolve to a canonical asset_id, and if it doesn't match anything known it
is flagged rather than silently invented. Here it runs read-only over the
persisted `asset_identifiers` + `unified_assets`, so the chatbot and RCA/
compliance workflows never reason about an asset that isn't grounded in the index.

Deterministic (no LLM call): exact identifier/name match first, then a bounded
partial (ILIKE) fallback.
"""

from __future__ import annotations

import asyncpg
from pydantic import BaseModel, Field


class AssetMatch(BaseModel):
    unified_id: str
    asset_name: str | None = None
    matched_on: str          # e.g. "serial_number=SN-100" or "name" or "partial:name"
    needs_review: bool = False


class MentionResolution(BaseModel):
    mention: str
    matches: list[AssetMatch] = Field(default_factory=list)
    resolved: bool = False   # exactly one confident match
    reason: str = ""

    @property
    def asset(self) -> AssetMatch | None:
        return self.matches[0] if self.resolved and self.matches else None


async def _exact(conn: asyncpg.Connection, mention: str) -> list[AssetMatch]:
    rows = await conn.fetch(
        """
        select ai.unified_id, ua.asset_name, ai.concept, ai.value, ua.needs_review
        from asset_identifiers ai
        join unified_assets ua on ua.unified_id = ai.unified_id
        where lower(ai.value) = lower($1)
        """,
        mention,
    )
    matches = [
        AssetMatch(
            unified_id=r["unified_id"],
            asset_name=r["asset_name"],
            matched_on=f'{r["concept"]}={r["value"]}',
            needs_review=r["needs_review"],
        )
        for r in rows
    ]
    name_rows = await conn.fetch(
        "select unified_id, asset_name, needs_review from unified_assets "
        "where lower(asset_name) = lower($1) or unified_id = $1",
        mention,
    )
    matches.extend(
        AssetMatch(
            unified_id=r["unified_id"],
            asset_name=r["asset_name"],
            matched_on="name",
            needs_review=r["needs_review"],
        )
        for r in name_rows
    )
    return matches


async def _partial(conn: asyncpg.Connection, mention: str, limit: int) -> list[AssetMatch]:
    like = "%" + mention.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = await conn.fetch(
        """
        select distinct ua.unified_id, ua.asset_name, ua.needs_review,
               min(ai.concept) as concept
        from unified_assets ua
        left join asset_identifiers ai on ai.unified_id = ua.unified_id
        where ua.asset_name ilike $1 or ai.value ilike $1
        group by ua.unified_id, ua.asset_name, ua.needs_review
        order by ua.unified_id
        limit $2
        """,
        like, limit,
    )
    return [
        AssetMatch(
            unified_id=r["unified_id"],
            asset_name=r["asset_name"],
            matched_on=f'partial:{r["concept"] or "name"}',
            needs_review=r["needs_review"],
        )
        for r in rows
    ]


def _dedupe(matches: list[AssetMatch]) -> list[AssetMatch]:
    seen: dict[str, AssetMatch] = {}
    for m in matches:
        seen.setdefault(m.unified_id, m)
    return list(seen.values())


async def resolve_mention(
    conn: asyncpg.Connection, mention: str, partial_limit: int = 5
) -> MentionResolution:
    """Resolve `mention` to a canonical unified asset (exact first, then partial)."""
    mention = (mention or "").strip()
    if not mention:
        return MentionResolution(mention=mention, reason="Empty mention.")

    exact = _dedupe(await _exact(conn, mention))
    if exact:
        return MentionResolution(
            mention=mention,
            matches=exact,
            resolved=len(exact) == 1 and not exact[0].needs_review,
            reason=(
                "Asset identity needs review before using combined evidence." if len(exact) == 1 and exact[0].needs_review else "" if len(exact) == 1
                else f"Ambiguous: {len(exact)} assets match '{mention}' exactly."
            ),
        )

    partial = _dedupe(await _partial(conn, mention, partial_limit))
    if not partial:
        return MentionResolution(
            mention=mention,
            reason=f"No asset in the index matches '{mention}'.",
        )
    return MentionResolution(
        mention=mention,
        matches=partial,
        resolved=False,
        reason=(
            "Matched by partial text; confirm this is the intended asset."
            if len(partial) == 1
            else f"Ambiguous: {len(partial)} possible assets for '{mention}'."
        ),
    )
