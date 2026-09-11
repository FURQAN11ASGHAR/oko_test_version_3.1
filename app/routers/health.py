from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import pool

router = APIRouter(prefix="/v1", tags=["health"])


@router.get("/health")
async def health():
    deps = {"database": "ok", "vector_index": "ok", "openrouter": "ok", "search_provider": "ok"}
    try:
        await pool().fetchval("SELECT 1")
    except Exception:
        deps["database"] = "down"
        deps["vector_index"] = "down"

    if not settings.openrouter_api_key:
        deps["openrouter"] = "degraded"
    # search_provider (ddgs) needs no config/API key, so it's always reported
    # "ok" here — a real outage only surfaces per-scan as live_lookup_failed.

    if deps["database"] == "down":
        status = "down"
    elif "degraded" in deps.values() or "down" in deps.values():
        status = "degraded"
    else:
        status = "ok"

    return JSONResponse(status_code=503 if status == "down" else 200, content={"status": status, "dependencies": deps, "version": "0.1.0"})
