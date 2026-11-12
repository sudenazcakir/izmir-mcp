"""İsteğe bağlı Supabase Postgres erişimi. DATABASE_URL yoksa her fonksiyon sessizce boş döner."""

import logging

import psycopg
from psycopg.rows import dict_row

from .config import get_settings
from .text import norm

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(get_settings().database_url)


async def search_stops(query: str, limit: int = 10) -> list[dict]:
    if not enabled():
        return []
    q = norm(query)
    try:
        async with await psycopg.AsyncConnection.connect(get_settings().database_url, row_factory=dict_row,
                                                         connect_timeout=5) as conn:
            cur = await conn.execute(
                """
                select id, name, lat, lon from stops
                where name_norm = %(q)s or name_norm like %(prefix)s
                order by (name_norm = %(q)s) desc, similarity(name_norm, %(q)s) desc
                limit %(limit)s
                """,
                {"q": q, "prefix": q + "%", "limit": limit},
            )
            return list(await cur.fetchall())
    except Exception as e:  # veritabanı çökse de araç canlı API ile devam eder
        log.warning("stop search failed: %r", e)
        return []


async def get_stop(stop_id: str) -> dict | None:
    if not enabled():
        return None
    try:
        async with await psycopg.AsyncConnection.connect(get_settings().database_url, row_factory=dict_row,
                                                         connect_timeout=5) as conn:
            cur = await conn.execute("select id, name, lat, lon from stops where id = %s", (stop_id,))
            return await cur.fetchone()
    except Exception as e:
        log.warning("get_stop failed: %r", e)
        return None
