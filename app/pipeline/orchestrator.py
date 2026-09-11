import asyncio
import hashlib
import logging

from ulid import ULID

from app import repo
from app.config import settings
from app.openrouter import ModelCallError, client
from app.pipeline.availability import check_availability
from app.pipeline.barcode import decode_gtin
from app.pipeline.image_resize import maybe_downscale_for_vision
from app.pipeline.relevance import FINDINGS_CODES, valid_finding_code
from app.pipeline.search import SearchUnavailable, fetch_page_text, is_trusted, search_web
from app.webhooks import deliver

logger = logging.getLogger("okotest")

# resolution reasons where nothing was confidently identified, or where the
# "identification" is a list of candidates rather than one product — no
# single product to check a buy-link for.
_SKIP_AVAILABILITY_REASONS = {"image_unreadable", "no_product_detected", "product_ambiguous"}

_EMPTY_PIPELINE = {"vision": None, "embedding": None, "extraction": None, "search": None}


class ScanFailed(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def run_scan(
    scan_id: str,
    image_bytes: bytes,
    mime: str,
    hint: str | None,
    allow_live_lookup: bool,
) -> None:
    """The resolution ladder (spec §1.1, §2). Runs as a background task; the
    caller polls GET /v1/scans/{id}. Every exit path writes the scan row so
    the poller always sees a terminal state.
    """
    pipeline_info = dict(_EMPTY_PIPELINE)
    warnings: list[dict] = []
    try:
        await repo.mark_processing(scan_id)
        sha256 = hashlib.sha256(image_bytes).hexdigest()

        prior = await repo.find_completed_scan_by_sha256(sha256)
        if prior and prior["scan_id"] != scan_id:
            await _finish_from_prior(scan_id, prior)
            return

        gtin = decode_gtin(image_bytes)
        if gtin:
            product = await repo.find_product_by_gtin(gtin)
            if product:
                await _finish_resolved(scan_id, "gtin_exact", 1.0, product["product_id"], pipeline_info, warnings)
                return

        try:
            vision_bytes, vision_mime, resized = maybe_downscale_for_vision(image_bytes, mime)
            descriptor, vms, vtokens = await client.vision_identify(vision_bytes, vision_mime, hint)
        except ModelCallError as e:
            raise ScanFailed("upstream_model_error", str(e))
        pipeline_info["vision"] = {
            "model": settings.vision_model,
            "provider": "openrouter",
            "ms": vms,
            "tokens": vtokens,
            "resized": resized,
        }

        if not descriptor.get("legible"):
            await _finish_unresolved(scan_id, "image_unreadable", pipeline_info, warnings)
            return
        is_report = bool(descriptor.get("is_test_report") and descriptor.get("test_subject"))
        if not descriptor.get("brand") and not descriptor.get("name") and not is_report:
            await _finish_unresolved(scan_id, "no_product_detected", pipeline_info, warnings)
            return

        identified_from = {
            "brand": descriptor.get("brand_confidence") or None,
            "name": descriptor.get("name_confidence") or None,
            "variant": descriptor.get("variant_confidence") or None,
            "gtin": None,  # gtin always comes from the decoder, never vision (spec §1.2)
        }
        pipeline_info["identified_from"] = identified_from
        id_confidence = _overall_identification_confidence(identified_from)
        if not is_report:
            # spec §4.2's unresolved example shows product with product_id:
            # null — vision identified something even when no catalog row
            # exists yet (or ever will, for a reason short of live lookup).
            pipeline_info["identified_product"] = {
                "brand": descriptor.get("brand"),
                "name": descriptor.get("name"),
                "variant": descriptor.get("variant"),
                "category": descriptor.get("category"),
            }

        other_products = [c for c in (descriptor.get("other_products") or []) if isinstance(c, dict) and (c.get("brand") or c.get("name"))]
        if other_products and not is_report:
            # spec §12 Q4 (previously open): several similarly-prominent
            # products in one photo, none clearly primary — surface every
            # candidate rather than silently guessing one (§4.2 suggested
            # client action: offer manual search per candidate).
            candidates = [
                {"brand": descriptor.get("brand"), "name": descriptor.get("name"), "category": descriptor.get("category")}
            ] + [{"brand": c.get("brand"), "name": c.get("name"), "category": c.get("category")} for c in other_products]
            pipeline_info["candidates"] = candidates
            await _finish_unresolved(scan_id, "product_ambiguous", pipeline_info, warnings, confidence=id_confidence)
            return

        if is_report:
            # the magazine issue itself, not a purchasable product — build the
            # embedding/search text from the test topic so a re-scan of the
            # same issue still matches at tier 2.
            descriptor_text = " ".join(filter(None, ["Öko-Test Testbericht", descriptor.get("test_subject")]))
        else:
            descriptor_text = " ".join(
                filter(None, [descriptor.get("brand"), descriptor.get("name"), descriptor.get("variant"), descriptor.get("category")])
            )

        try:
            embedding, ems = await client.embed(descriptor_text, kind="query")
        except ModelCallError as e:
            raise ScanFailed("upstream_model_error", str(e))
        pipeline_info["embedding"] = {"model": settings.embedding_model, "provider": "openrouter", "ms": ems}

        matches = await repo.vector_search(embedding, threshold=settings.vector_match_threshold, limit=1)
        if matches:
            match = matches[0]
            await _finish_resolved(scan_id, "vector_match", float(match["score"]), match["product_id"], pipeline_info, warnings)
            return

        if not allow_live_lookup:
            await _finish_unresolved(scan_id, "live_lookup_disabled", pipeline_info, warnings, confidence=id_confidence)
            return

        await _run_live_lookup(scan_id, descriptor, descriptor_text, embedding, gtin, is_report, id_confidence, pipeline_info, warnings)

    except ScanFailed as e:
        warnings.append({"code": e.code, "message": str(e)})
        await repo.update_scan_result(
            scan_id=scan_id,
            status="failed",
            resolution_tier=None,
            resolution_reason=e.code,
            product_id=None,
            confidence=None,
            pipeline=pipeline_info,
            warnings=warnings,
        )
    except Exception:  # noqa: BLE001 - deliberate catch-all
        # Anything not already a ScanFailed — e.g. a corrupted/truncated
        # image raising PIL.UnidentifiedImageError out of decode_gtin()
        # before any of this function's own error handling has a chance to
        # run. Without this, run_scan (fired via asyncio.create_task, so
        # nothing awaits it directly) would raise silently into the event
        # loop's default handler and the scan row would stay "processing"
        # forever — polling GET /v1/scans/{id} would just hang with no
        # error ever surfaced to the client. A scan must always reach a
        # terminal status.
        logger.exception("scan %s failed with an unexpected error", scan_id)
        warnings.append({"code": "internal_error", "message": "An unexpected error occurred while processing this scan."})
        await repo.update_scan_result(
            scan_id=scan_id,
            status="failed",
            resolution_tier=None,
            resolution_reason="internal_error",
            product_id=None,
            confidence=None,
            pipeline=pipeline_info,
            warnings=warnings,
        )
    finally:
        await _maybe_fire_webhook(scan_id)


async def _run_live_lookup(
    scan_id: str,
    descriptor: dict,
    descriptor_text: str,
    embedding: list[float],
    gtin: str | None,
    is_test_report: bool,
    id_confidence: float | None,
    pipeline_info: dict,
    warnings: list[dict],
) -> None:
    if is_test_report:
        search_query = descriptor.get("test_subject") or descriptor.get("name") or ""
    else:
        # category is useful signal for the embedding (semantic match,
        # language-agnostic) but often an English label from the vision
        # model — including it as literal search keywords hurts recall
        # against German-language oekotest.de content, so drop it here.
        search_query = " ".join(filter(None, [descriptor.get("brand"), descriptor.get("name"), descriptor.get("variant")]))
    try:
        results = await search_web(f"{search_query} Öko-Test")
    except SearchUnavailable as e:
        warnings.append({"code": "search_unavailable", "message": str(e)})
        await _finish_unresolved(scan_id, "live_lookup_failed", pipeline_info, warnings, confidence=id_confidence)
        return
    pipeline_info["search"] = {"provider": "ddgs", "ms": None, "result_count": len(results)}

    # A search snippet rarely contains the actual verdict — it lives in the
    # article body. Fetch full text for trusted-domain hits only (cheap: at
    # most a couple of oekotest.de pages) so extraction has something to cite.
    for r in results:
        if is_trusted(r["url"]):
            full_text = await fetch_page_text(r["url"])
            if full_text:
                r["snippet"] = full_text

    try:
        extraction, ems = await client.extract_test_result(results, descriptor_text, is_test_report=is_test_report)
    except ModelCallError as e:
        warnings.append({"code": "upstream_model_error", "message": str(e)})
        await _finish_unresolved(scan_id, "live_lookup_failed", pipeline_info, warnings, confidence=id_confidence)
        return
    pipeline_info["extraction"] = {"model": settings.extraction_model, "provider": "openrouter", "ms": ems}

    # the model transcribes URLs by hand and can introduce typos in the path
    # (hostname-only is_trusted() wouldn't catch that); only URLs verbatim in
    # what we actually handed it are safe to persist as a citation.
    seen_urls = {r["url"] for r in results}

    general_info = None
    if extraction.get("general_description") or extraction.get("general_features"):
        general_info = {
            "description": extraction.get("general_description") or None,
            "features": [f for f in (extraction.get("general_features") or []) if isinstance(f, str)],
            "source_urls": [u for u in (extraction.get("general_source_urls") or []) if isinstance(u, str) and u in seen_urls],
            "disclaimer": "General product information gathered from public web sources. Not an Öko-Test finding and not verified or endorsed by Öko-Test.",
        }

    product_id = f"prd_{ULID()}"
    await repo.insert_product(
        product_id=product_id,
        brand="Öko-Test" if is_test_report else (descriptor.get("brand") or "Unknown"),
        name=(descriptor.get("test_subject") or descriptor.get("name") or "Unknown") if is_test_report else (descriptor.get("name") or "Unknown"),
        variant=descriptor.get("variant"),
        category="Öko-Test Testbericht" if is_test_report else descriptor.get("category"),
        gtins=[gtin] if gtin else [],
        descriptor=descriptor_text,
        embedding=embedding,
        embedding_model=settings.embedding_model,
        origin="live_lookup",
        verified=False,
        general_info=general_info,
    )

    if is_test_report:
        report_product_ids = await _write_back_report_products(
            extraction.get("products") or [],
            seen_urls,
            test_date=extraction.get("test_date"),
            issue=extraction.get("issue"),
            test_name=extraction.get("test_name"),
        )
        if report_product_ids:
            pipeline_info["report"] = {
                "subject": descriptor.get("test_subject"),
                "test_name": extraction.get("test_name"),
                "test_date": extraction.get("test_date"),
                "issue": extraction.get("issue"),
                "product_ids": report_product_ids,
            }
            await _finish_resolved(scan_id, "live_lookup", None, product_id, pipeline_info, warnings)
            return
        # no individual products cleared write-back — fall through to the
        # ordinary found/verdict handling below (rare: report mode but the
        # model only surfaced a single top-level verdict, or nothing at all).

    if not extraction.get("found"):
        # positive finding (spec §4.2): identified, no test exists. Cached
        # with a short TTL (UNTESTED_RECHECK_DAYS) so the next scan resolves
        # at tier 2 instead of paying for another live lookup.
        await _finish_unresolved(scan_id, "product_untested", pipeline_info, warnings, product_id=product_id, confidence=id_confidence)
        return

    verdict = extraction.get("verdict")
    confidence = float(extraction.get("confidence") or 0.0)
    source_urls = [u for u in (extraction.get("source_urls") or []) if isinstance(u, str) and u in seen_urls]
    trusted_urls = [u for u in source_urls if is_trusted(u)]

    # write-back rules, spec §2.1 — all four must hold or nothing is persisted
    # as authoritative. (Conflict-with-existing-record check is a no-op here
    # since this product_id is brand new; re-verification of an existing
    # catalog product against a later issue is not wired in this MVP.)
    passes = bool(verdict) and bool(trusted_urls) and confidence >= settings.write_back_min_confidence

    if not passes:
        warnings.append(
            {
                "code": "not_write_back_eligible",
                "message": "Live lookup result did not meet write-back rules (verdict/trusted source/confidence); not persisted as authoritative.",
            }
        )
        await _finish_unresolved(scan_id, "live_lookup_failed", pipeline_info, warnings, product_id=product_id, confidence=id_confidence)
        return

    findings = await _validate_findings(extraction.get("findings") or [], seen_urls)

    result_id = f"res_{ULID()}"
    await repo.insert_test_result(
        result_id=result_id,
        product_id=product_id,
        verdict=verdict,
        test_date=extraction.get("test_date"),
        issue=extraction.get("issue"),
        test_name=extraction.get("test_name"),
        findings=findings,
        source_urls=source_urls,
        extraction_model=settings.extraction_model,
        extraction_confidence=confidence,
        origin="live_lookup",
    )
    await _finish_resolved(scan_id, "live_lookup", confidence, product_id, pipeline_info, warnings)


async def _validate_findings(raw_findings: list, seen_urls: set[str]) -> list[dict]:
    """spec §7.3: only recognized codes get persisted; anything else goes to
    review_queue instead of being silently dropped or trusted."""
    cleaned = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        source_url = item.get("source_url")
        if source_url is not None and source_url not in seen_urls:
            source_url = None  # same anti-hallucination rule as verdict source_urls
        if valid_finding_code(code):
            cleaned.append(
                {
                    "code": code,
                    "label": FINDINGS_CODES[code].get("label"),
                    "as_reported": item.get("as_reported"),
                    "deduction": item.get("deduction") if item.get("deduction") in ("verdict_impacting", "informational") else "informational",
                    "source_url": source_url,
                }
            )
        else:
            await repo.enqueue_review(review_id=f"rvw_{ULID()}", kind="bad_vocab", payload={"item": item})
    return cleaned


async def _write_back_report_products(
    items: list[dict], seen_urls: set[str], *, test_date: str | None, issue: str | None, test_name: str | None
) -> list[str]:
    """Same write-back rules as a single-product lookup (spec §2.1), applied
    per product in a multi-product test report. Returns the product_ids that
    were actually persisted."""
    written: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        brand, name, verdict = item.get("brand"), item.get("name"), item.get("verdict")
        confidence = float(item.get("confidence") or 0.0)
        source_urls = [u for u in (item.get("source_urls") or []) if isinstance(u, str) and u in seen_urls]
        trusted_urls = [u for u in source_urls if is_trusted(u)]
        if not (brand and name and verdict and trusted_urls and confidence >= settings.write_back_min_confidence):
            continue
        item_product_id = f"prd_{ULID()}"
        await repo.insert_product(
            product_id=item_product_id,
            brand=brand,
            name=name,
            variant=item.get("variant"),
            category=None,
            gtins=[],
            descriptor=" ".join(filter(None, [brand, name, item.get("variant")])),
            embedding=None,  # not independently photographed; a future direct scan will embed it properly
            embedding_model=None,
            origin="live_lookup",
            verified=False,
        )
        await repo.insert_test_result(
            result_id=f"res_{ULID()}",
            product_id=item_product_id,
            verdict=verdict,
            test_date=test_date,
            issue=issue,
            test_name=test_name,
            findings=[],
            source_urls=source_urls,
            extraction_model=settings.extraction_model,
            extraction_confidence=confidence,
            origin="live_lookup",
        )
        written.append(item_product_id)
    return written


async def _attach_availability(pipeline_info: dict, warnings: list[dict], *, product_id: str | None) -> None:
    """Separate, text-only, second LLM call — deliberately never given the
    photo or the raw vision output, only the already-identified product
    fields, so a vision hallucination can't leak into a buy-link claim (see
    app/pipeline/availability.py). Runs for any identified product,
    resolved or not, except the reasons in _SKIP_AVAILABILITY_REASONS where
    nothing was confidently identified in the first place.
    """
    brand = name = variant = gtin = None
    if product_id:
        row = await repo.get_product(product_id)
        if row:
            brand, name, variant = row["brand"], row["name"], row["variant"]
            gtin = row["gtins"][0] if row.get("gtins") else None
    elif pipeline_info.get("identified_product"):
        ip = pipeline_info["identified_product"]
        brand, name, variant = ip.get("brand"), ip.get("name"), ip.get("variant")

    if not brand and not name:
        return  # nothing to check

    try:
        pipeline_info["availability"] = await check_availability(brand=brand, name=name, variant=variant, gtin=gtin)
    except Exception as e:  # noqa: BLE001 - never let this feature fail the whole scan
        warnings.append({"code": "availability_check_failed", "message": str(e)})


async def _finish_resolved(scan_id, tier, confidence, product_id, pipeline_info, warnings) -> None:
    await _attach_availability(pipeline_info, warnings, product_id=product_id)
    await repo.bump_scan_count(product_id)
    await repo.update_scan_result(
        scan_id=scan_id,
        status="completed",
        resolution_tier=tier,
        resolution_reason=None,
        product_id=product_id,
        confidence=confidence,
        pipeline=pipeline_info,
        warnings=warnings,
    )


async def _finish_unresolved(scan_id, reason, pipeline_info, warnings, product_id=None, confidence=None) -> None:
    if reason not in _SKIP_AVAILABILITY_REASONS:
        await _attach_availability(pipeline_info, warnings, product_id=product_id)
    await repo.update_scan_result(
        scan_id=scan_id,
        status="completed",
        resolution_tier="unresolved",
        resolution_reason=reason,
        product_id=product_id,
        confidence=confidence,
        pipeline=pipeline_info,
        warnings=warnings,
    )


def _overall_identification_confidence(identified_from: dict) -> float | None:
    """spec §4.2's unresolved example shows a numeric confidence even without
    a catalog match — the vision model's own confidence in what it read."""
    values = [v for v in (identified_from.get("brand"), identified_from.get("name")) if isinstance(v, (int, float)) and v > 0]
    return round(sum(values) / len(values), 4) if values else None


async def _finish_from_prior(scan_id: str, prior: dict) -> None:
    pipeline_info = dict(_EMPTY_PIPELINE)
    warnings: list[dict] = []
    if prior["resolution_reason"] not in _SKIP_AVAILABILITY_REASONS:
        # Re-checked fresh here too (never cached) — a repeat scan of the
        # same photo still gets a current buy-link check, since availability
        # is exactly the kind of thing that goes stale between scans.
        await _attach_availability(pipeline_info, warnings, product_id=prior["product_id"])
    await repo.update_scan_result(
        scan_id=scan_id,
        status="completed",
        resolution_tier="image_cache",
        resolution_reason=prior["resolution_reason"],
        product_id=prior["product_id"],
        confidence=prior["confidence"],
        pipeline=pipeline_info,
        warnings=warnings,
    )
    if prior["product_id"]:
        await repo.bump_scan_count(prior["product_id"])


async def _maybe_fire_webhook(scan_id: str) -> None:
    scan = await repo.get_scan(scan_id)
    if not scan or not scan["webhook_url"]:
        return
    from app.schemas import serialize_scan  # local import: avoid a circular import with repo/schemas

    body = await serialize_scan(scan)
    event = "scan.completed" if scan["status"] == "completed" else "scan.failed"
    asyncio.create_task(deliver(scan["webhook_url"], event, body))