import hashlib
from dataclasses import dataclass

from fastapi import Header

from app.db import pool
from app.errors import insufficient_scope, unauthenticated


@dataclass
class ApiKey:
    key_id: str
    scopes: list[str]


def require_scope(scope: str):
    async def dep(x_api_key: str | None = Header(default=None)) -> ApiKey:
        if not x_api_key:
            raise unauthenticated()
        key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()
        row = await pool().fetchrow(
            "SELECT key_id, scopes FROM api_keys WHERE key_hash = $1 AND revoked_at IS NULL",
            key_hash,
        )
        if row is None:
            raise unauthenticated()
        if scope not in row["scopes"]:
            raise insufficient_scope(scope)
        return ApiKey(key_id=row["key_id"], scopes=row["scopes"])

    return dep
