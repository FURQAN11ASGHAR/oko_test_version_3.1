import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    return _pool


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call init_pool() at startup")
    return _pool


def to_pgvector(values: list[float]) -> str:
    """asyncpg has no built-in pgvector codec; pass as text and cast ::vector in SQL."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def from_pgvector(text: str | None) -> list[float] | None:
    if not text:
        return None
    return [float(v) for v in text.strip("[]").split(",") if v]
