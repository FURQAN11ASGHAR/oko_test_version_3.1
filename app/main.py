import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app import repo
from app.config import assert_boot_invariants, settings
from app.db import close_pool, init_pool
from app.errors import ApiError, api_error_handler
from app.openrouter import client
from app.routers import health, products, scans

logger = logging.getLogger("okotest")

_PURGE_INTERVAL_S = 3600


async def _purge_expired_scans_loop() -> None:
    """spec §4.2: scans are retained for SCAN_RETENTION_DAYS, then the scan
    record (not the derived catalog entry) goes away. GET already reports
    'expired' past that age; this actually deletes the rows."""
    while True:
        try:
            deleted = await repo.purge_expired_scans(settings.scan_retention_days)
            if deleted:
                logger.info("purged %d expired scan(s)", deleted)
        except Exception:
            logger.exception("scan purge loop failed")
        await asyncio.sleep(_PURGE_INTERVAL_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    assert_boot_invariants()
    await init_pool()
    purge_task = asyncio.create_task(_purge_expired_scans_loop())
    yield
    purge_task.cancel()
    await client.aclose()
    await close_pool()


app = FastAPI(title="Öko-Test Lookup API", version="0.1.0", lifespan=lifespan)
app.add_exception_handler(ApiError, api_error_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return api_error_handler(request, ApiError(500, "internal_error", "An unexpected error occurred."))


app.include_router(scans.router)
app.include_router(products.router)
app.include_router(health.router)

_TEST_PAGE = Path(__file__).resolve().parent.parent / "web" / "index.html"


@app.get("/test", include_in_schema=False)
async def test_page():
    # this file has changed repeatedly during development; a stale cached
    # copy in the browser silently running old JS has caused real confusion
    # (e.g. a fixed allow_live_lookup bug appearing to still happen).
    return FileResponse(_TEST_PAGE, headers={"Cache-Control": "no-store"})
