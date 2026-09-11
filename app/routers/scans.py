import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from ulid import ULID

from app import repo
from app.auth import ApiKey, require_scope
from app.config import settings
from app.errors import ApiError, invalid_request, not_found
from app.pipeline.image_security import UnsafeImageError, validate_image_security
from app.pipeline.orchestrator import run_scan
from app.ratelimit import check_live_lookup_budget, check_read_limit, check_scan_limits
from app.schemas import ContextModel, ScanCreateJSON, serialize_scan

router = APIRouter(prefix="/v1/scans", tags=["scans"])

_MAGIC: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
]


class FeedbackRequest(BaseModel):
    kind: Literal["wrong_product", "wrong_test_result", "outdated_result", "product_not_found", "other"]
    correct_gtin: str | None = None
    comment: str | None = None


def _sniff_mime(data: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_expired(scan: dict) -> bool:
    age = datetime.now(timezone.utc) - scan["created_at"]
    return age > timedelta(days=settings.scan_retention_days)


async def _parse_request(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("image")
        if upload is None or isinstance(upload, str):
            raise invalid_request("`image` field is required")
        image_bytes = await upload.read()
        raw_context = form.get("context")
        context = ContextModel.model_validate(json.loads(raw_context)).model_dump(exclude_none=True) if raw_context else None
        return {
            "image_bytes": image_bytes,
            "hint": form.get("hint"),
            "locale": form.get("locale") or "de-DE",
            "webhook_url": form.get("webhook_url") or None,
            "allow_live_lookup": str(form.get("allow_live_lookup", "true")).lower() != "false",
            "context": context,
        }

    if content_type.startswith("application/json"):
        try:
            raw = await request.json()
        except json.JSONDecodeError:
            raise invalid_request("body is not valid JSON")
        payload = ScanCreateJSON.model_validate(raw)
        try:
            import base64

            image_bytes = base64.b64decode(payload.image_base64, validate=True)
        except Exception:
            raise invalid_request("image_base64 is not valid base64")
        return {
            "image_bytes": image_bytes,
            "hint": payload.hint,
            "locale": payload.locale,
            "webhook_url": payload.webhook_url,
            "allow_live_lookup": payload.allow_live_lookup,
            "context": payload.context.model_dump(exclude_none=True) if payload.context else None,
        }

    raise invalid_request("Content-Type must be multipart/form-data or application/json")


@router.post("")
async def create_scan(request: Request, key: ApiKey = Depends(require_scope("scan"))):
    parsed = await _parse_request(request)
    image_bytes: bytes = parsed["image_bytes"]

    if not image_bytes:
        raise invalid_request("image is required")
    if len(image_bytes) > settings.image_max_bytes:
        raise ApiError(
            400,
            "image_too_large",
            f"Image exceeds the {settings.image_max_bytes} byte limit.",
            {"size_bytes": len(image_bytes), "limit_bytes": settings.image_max_bytes},
        )
    mime = _sniff_mime(image_bytes)
    if mime is None:
        raise ApiError(400, "unsupported_media_type", "Image must be JPEG, PNG or WebP.")

    # Fail fast, reject outright — run before rate limiting or scan creation
    # even happens, so a malicious upload never costs a scan slot or touches
    # the DB. Catches trailing/appended payloads (polyglot files) and
    # malformed/oversized structure (see app/pipeline/image_security.py).
    try:
        validate_image_security(image_bytes, mime)
    except UnsafeImageError as e:
        raise ApiError(400, "unsafe_image", e.message)

    rl = check_scan_limits(key.key_id, per_min=settings.scan_rate_limit_per_min, per_day=settings.scan_rate_limit_per_day)

    allow_live_lookup = parsed["allow_live_lookup"]
    if allow_live_lookup:
        try:
            check_live_lookup_budget(key.key_id, per_day=settings.live_lookup_rate_limit_per_day)
        except ApiError:
            allow_live_lookup = False  # budget exhausted: degrade to tiers 0-2 (spec §9), don't error

    scan_id = f"scn_{ULID()}"
    await repo.create_scan(
        scan_id=scan_id,
        api_key_id=key.key_id,
        image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        hint=parsed["hint"],
        locale=parsed["locale"],
        webhook_url=parsed["webhook_url"],
        allow_live_lookup=allow_live_lookup,
        context=parsed["context"],
    )

    task = asyncio.create_task(run_scan(scan_id, image_bytes, mime, parsed["hint"], allow_live_lookup))
    # Give tiers 0-2 a chance to finish inline (typically <5s) so cache/GTIN/
    # vector hits can return 200 synchronously per spec §4.1; tier 3 (up to
    # ~30s) falls through to the 202 + poll path.
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=4.0)
    except asyncio.TimeoutError:
        pass

    scan = await repo.get_scan(scan_id)
    body = await serialize_scan(scan)
    if scan["status"] in ("completed", "failed"):
        return JSONResponse(status_code=200, content=body, headers=rl.headers())

    poll_url = str(request.base_url).rstrip("/") + f"/v1/scans/{scan_id}"
    body.update({"poll_url": poll_url, "estimated_ms": 12000 if allow_live_lookup else 3000})
    return JSONResponse(status_code=202, content=body, headers=rl.headers())


@router.get("/{scan_id}")
async def get_scan(scan_id: str, response: Response, key: ApiKey = Depends(require_scope("read"))):
    rl = check_read_limit(key.key_id, per_min=settings.read_rate_limit_per_min)
    response.headers.update(rl.headers())
    scan = await repo.get_scan(scan_id)
    if not scan:
        raise not_found("scan_id")
    if scan["status"] in ("completed", "failed") and _is_expired(scan):
        # spec §4.2: the derived catalog entry persists, the scan record does not.
        return {"scan_id": scan["scan_id"], "status": "expired", "created_at": scan["created_at"].isoformat()}
    return await serialize_scan(scan)


@router.post("/{scan_id}/feedback", status_code=202)
async def submit_feedback(scan_id: str, body: FeedbackRequest, key: ApiKey = Depends(require_scope("feedback"))):
    scan = await repo.get_scan(scan_id)
    if not scan:
        raise not_found("scan_id")
    feedback_id = f"fbk_{ULID()}"
    await repo.insert_feedback(feedback_id=feedback_id, scan_id=scan_id, kind=body.kind, payload=body.model_dump(exclude_none=True))
    return {"feedback_id": feedback_id, "status": "recorded"}