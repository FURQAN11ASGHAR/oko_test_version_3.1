"""Issue a local API key. Usage: python scripts/create_api_key.py scan read feedback"""

import asyncio
import hashlib
import secrets
import sys

import asyncpg
from ulid import ULID

sys.path.insert(0, ".")
from app.config import settings  # noqa: E402


async def main(scopes: list[str]) -> None:
    raw_key = "ok_live_" + secrets.token_hex(24)
    key_id = f"key_{ULID()}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    conn = await asyncpg.connect(settings.database_url)
    try:
        await conn.execute(
            "INSERT INTO api_keys (key_id, key_hash, scopes) VALUES ($1, $2, $3)",
            key_id,
            key_hash,
            scopes,
        )
    finally:
        await conn.close()

    print(f"key_id:  {key_id}")
    print(f"scopes:  {scopes}")
    print(f"API key: {raw_key}")
    print("(the raw key is shown once — only its hash is stored)")


if __name__ == "__main__":
    scopes = sys.argv[1:] or ["scan", "read", "feedback"]
    asyncio.run(main(scopes))
