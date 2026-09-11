import json

from pydantic import BaseModel

from app import repo
from app.pipeline.relevance import compute_context_relevance

VERDICT_LABELS = {
    "sehr_gut": "sehr gut",
    "gut": "gut",
    "befriedigend": "befriedigend",
    "ausreichend": "ausreichend",
    "mangelhaft": "mangelhaft",
    "ungenuegend": "ungenügend",
}

REASON_MESSAGES = {
    "image_unreadable": "Blur, glare or crop prevented identification. Ask for another photo.",
    "no_product_detected": "The image does not appear to contain a product. Ask for another photo.",
    "product_ambiguous": "Several candidate products were found, none confident enough.",
    "product_untested": "The product was identified but no Öko-Test result was found for it.",
    "live_lookup_disabled": "Live lookup is disabled or its daily budget is exhausted. Retryable.",
    "live_lookup_failed": "Web search or extraction failed or did not meet write-back rules. Retryable.",
}

_RESOLVED_TIERS = {"image_cache", "gtin_exact", "vector_match"}

# Reasons where nothing was identified at all — a spam/junk/non-product photo.
# There's no product, no candidates, nothing partial worth building the full
# nested response for, so these get a short, direct error instead (see
# serialize_scan below). Every other reason (product_untested,
# product_ambiguous, live_lookup_disabled/failed) keeps the full response —
# those DO carry real partial data (identified product, candidates, etc.)
# that a client can actually use.
_HARD_FAILURE_REASONS = {"image_unreadable", "no_product_detected"}


class ContextModel(BaseModel):
    intended_use: list[str] | None = None
    application: str | None = None
    frequency: str | None = None
    environment: str | None = None
    notes: str | None = None


class ScanCreateJSON(BaseModel):
    image_base64: str
    hint: str | None = None
    locale: str = "de-DE"
    webhook_url: str | None = None
    allow_live_lookup: bool = True
    context: ContextModel | None = None


def _jsonb(v):
    if v is None or isinstance(v, (dict, list)):
        return v
    return json.loads(v)


def _serialize_product_ref(p: dict) -> dict:
    return {
        "product_id": p["product_id"],
        "brand": p["brand"],
        "name": p["name"],
        "variant": p["variant"],
        "category": p["category"],
        "gtins": p["gtins"],
        "general_info": _jsonb(p.get("general_info")) if p.get("general_info") else None,
    }


def _serialize_test_result(r: dict) -> dict:
    return {
        "result_id": r["result_id"],
        "verdict": r["verdict"],
        "verdict_label": VERDICT_LABELS.get(r["verdict"], r["verdict"]),
        "test_date": r["test_date"],
        "publication": {"issue": r["issue"], "test_name": r["test_name"]},
        "source_urls": r["source_urls"],
        "findings": _jsonb(r["findings"]) or [],
        "retrieved_at": r["retrieved_at"].isoformat(),
        "is_current": r["is_current"],
    }


async def serialize_scan(scan: dict) -> dict:
    body = {
        "scan_id": scan["scan_id"],
        "status": scan["status"],
        "created_at": scan["created_at"].isoformat(),
    }
    if scan["completed_at"]:
        body["completed_at"] = scan["completed_at"].isoformat()
        body["duration_ms"] = int((scan["completed_at"] - scan["created_at"]).total_seconds() * 1000)

    if scan["status"] not in ("completed", "failed"):
        body["error"] = False
        return body

    if scan["status"] == "failed":
        # A real system failure (ScanFailed in orchestrator.py) — the actual
        # human-readable message was recorded in warnings at failure time.
        warnings = list(_jsonb(scan["warnings"]) or [])
        reason = scan["resolution_reason"] or "internal_error"
        message = next(
            (w["message"] for w in warnings if w.get("code") == reason),
            "The scan could not be completed. Please try again.",
        )
        body["error"] = True
        body["reason"] = reason
        body["message"] = message
        return body

    if scan["resolution_reason"] in _HARD_FAILURE_REASONS:
        # Spam/non-product/unreadable photo: skip building product,
        # test_results, pipeline, etc. entirely — there's nothing in them
        # but nulls anyway — and return a short, direct error message
        # instead of the full response shape.
        reason = scan["resolution_reason"]
        body["error"] = True
        body["reason"] = reason
        body["message"] = REASON_MESSAGES[reason]
        return body

    body["error"] = False
    pipeline = _jsonb(scan["pipeline"]) or {}

    product = None
    test_results: list[dict] = []
    if scan["product_id"]:
        prod_row = await repo.get_product(scan["product_id"])
        if prod_row:
            product = _serialize_product_ref(prod_row)
            rows = await repo.get_current_test_results(scan["product_id"])
            test_results = [_serialize_test_result(r) for r in rows]
    elif pipeline.get("identified_product"):
        # spec §4.2's unresolved example: vision identified something even
        # though no catalog row exists (product_id: null) — e.g. live lookup
        # was disabled before ever reaching the point of creating one.
        product = {**pipeline["identified_product"], "product_id": None, "gtins": [], "general_info": None}

    tier = scan["resolution_tier"] or "unresolved"
    verified = tier in _RESOLVED_TIERS or (tier == "live_lookup" and scan["resolution_reason"] is None)
    resolution = {"tier": tier, "confidence": scan["confidence"], "verified": verified}
    if scan["resolution_reason"]:
        resolution["reason"] = scan["resolution_reason"]

    warnings = list(_jsonb(scan["warnings"]) or [])
    if scan["resolution_reason"] in REASON_MESSAGES and not any(w.get("code") == scan["resolution_reason"] for w in warnings):
        warnings.append({"code": scan["resolution_reason"], "message": REASON_MESSAGES[scan["resolution_reason"]]})

    if product and pipeline.get("identified_from"):
        # spec §4.2's resolved example: per-field vision confidence, only
        # meaningful when this scan actually ran vision (not a tier 0/1 hit).
        product["identified_from"] = pipeline["identified_from"]

    body.update(
        {
            "resolution": resolution,
            "product": product,
            "test_results": test_results,
            "pipeline": pipeline,
            "warnings": warnings,
        }
    )

    if pipeline.get("availability"):
        # Mirrors the same pattern as candidates/test_report below: the
        # pipeline dict is where a background-task result gets stashed
        # (app/pipeline/orchestrator.py::_attach_availability), and the
        # response surfaces the useful part of it as a top-level field.
        body["availability"] = pipeline["availability"]

    if pipeline.get("candidates"):
        # spec §12 Q4 (product_ambiguous): several similarly-prominent
        # products, none confident enough to commit to — surface every
        # candidate so the client can offer manual search (§4.4) per one.
        body["candidates"] = pipeline["candidates"]

    report = pipeline.get("report")
    if report and report.get("product_ids"):
        report_products = []
        for pid in report["product_ids"]:
            row = await repo.get_product(pid)
            if not row:
                continue
            current = await repo.get_current_test_results(pid)
            entry = _serialize_product_ref(row)
            entry["test_results"] = [_serialize_test_result(r) for r in current]
            report_products.append(entry)
        body["test_report"] = {
            "subject": report.get("subject"),
            "test_name": report.get("test_name"),
            "test_date": report.get("test_date"),
            "issue": report.get("issue"),
            "products": report_products,
        }

    context = _jsonb(scan["context"])
    if context:
        findings = [f for r in test_results for f in r.get("findings", [])]
        body["context_relevance"] = compute_context_relevance(findings, context)

    return body


def serialize_product_detail(p: dict, test_results: list[dict]) -> dict:
    ref = _serialize_product_ref(p)
    ref.update(
        {
            "created_at": p["created_at"].isoformat(),
            "updated_at": p["updated_at"].isoformat(),
            "verified": p["verified"],
            "scan_count": p["scan_count"],
        }
    )
    return {"product": ref, "test_results": [_serialize_test_result(r) for r in test_results]}