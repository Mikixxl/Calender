"""Async Postgres access via asyncpg.

The backend connects as the database owner, which bypasses RLS, so the
locked-down `sched` tables are fully reachable here and nowhere else.
statement_cache_size=0 keeps us safe behind the Supabase transaction pooler.
"""
import json

import asyncpg

from .config import settings

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    # Decode jsonb to python objects (and back), so `answers` is a dict.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=1,
            max_size=5,
            statement_cache_size=0,
            init=_init_conn,
            server_settings={"search_path": "sched,public"},
        )
    return _pool


async def fetch(query: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def execute(query: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)
