"""Indexed PDF passages; source bytes remain in the custody record."""

INDEX_SQL = """
create table if not exists document_chunks (
    document_id bigint not null references document_parses(id) on delete cascade,
    chunk_index int not null,
    page int,
    section text not null default '',
    text text not null,
    tsv tsvector generated always as (to_tsvector('english', text) || to_tsvector('simple', text)) stored,
    primary key (document_id, chunk_index)
);
create index if not exists document_chunks_tsv_idx on document_chunks using gin(tsv);
"""


async def initialize_document_index(pool):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(INDEX_SQL)
            # Backfill existing uploads without changing their source or asset records.
            await conn.execute("""
                insert into document_chunks(document_id, chunk_index, page, section, text)
                select d.id, (c->>'index')::int, (c->>'page')::int, coalesce(c->>'section',''), c->>'text'
                from document_parses d cross join lateral jsonb_array_elements(d.parsed->'chunks') c
                where length(c->>'text') > 0
                on conflict do nothing
            """)
            await conn.execute("""
                insert into connectors(name,description,kind,status)
                select distinct system,'Document (PDF)','document','idle' from document_parses
                on conflict(name) do nothing
            """)


async def replace_document_chunks(conn, document_id, parsed):
    await conn.execute("delete from document_chunks where document_id=$1", document_id)
    await conn.executemany(
        "insert into document_chunks(document_id,chunk_index,page,section,text) values($1,$2,$3,$4,$5)",
        [(document_id, c.index, c.page, c.section, c.text) for c in parsed.chunks if c.text.strip()],
    )
