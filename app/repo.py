import json

from app.db import from_pgvector, pool, to_pgvector


async def find_product_by_gtin(gtin: str) -> dict | None:
    return await pool().fetchrow(
        """
        SELECT p.* FROM products p
        JOIN product_gtins g ON g.product_id = p.product_id
        WHERE g.gtin = $1
        """,
        gtin,
    )


async def vector_search(embedding: list[float], *, threshold: float, limit: int = 1) -> list[dict]:
    rows = await pool().fetch(
        """
        SELECT *, 1 - (embedding <=> $1::vector) AS score
        FROM products
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> $1::vector
        LIMIT $2
        """,
        to_pgvector(embedding),
        limit,
    )
    return [r for r in rows if r["score"] >= threshold]


async def get_product(product_id: str) -> dict | None:
    return await pool().fetchrow("SELECT * FROM products WHERE product_id = $1", product_id)


async def get_current_test_results(product_id: str) -> list[dict]:
    return await pool().fetch(
        "SELECT * FROM test_results WHERE product_id = $1 AND is_current = true ORDER BY test_date DESC",
        product_id,
    )


async def insert_product(
    *,
    product_id: str,
    brand: str,
    name: str,
    variant: str | None,
    category: str | None,
    gtins: list[str],
    descriptor: str,
    embedding: list[float] | None,
    embedding_model: str | None,
    origin: str,
    verified: bool,
    general_info: dict | None = None,
) -> None:
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO products (
                    product_id, brand, name, variant, category, gtins,
                    descriptor, embedding, embedding_model, embedding_version,
                    origin, verified, general_info
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector,$9,'v1',$10,$11,$12)
                """,
                product_id,
                brand,
                name,
                variant,
                category,
                gtins,
                descriptor,
                to_pgvector(embedding) if embedding else None,
                embedding_model,
                origin,
                verified,
                json.dumps(general_info) if general_info else None,
            )
            for gtin in gtins:
                await conn.execute(
                    "INSERT INTO product_gtins (gtin, product_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                    gtin,
                    product_id,
                )


async def bump_scan_count(product_id: str) -> None:
    await pool().execute(
        "UPDATE products SET scan_count = scan_count + 1, updated_at = now() WHERE product_id = $1",
        product_id,
    )


async def insert_test_result(
    *,
    result_id: str,
    product_id: str,
    verdict: str,
    test_date: str | None,
    issue: str | None,
    test_name: str | None,
    findings: list[dict],
    source_urls: list[str],
    extraction_model: str,
    extraction_confidence: float,
    origin: str,
) -> None:
    await pool().execute(
        """
        INSERT INTO test_results (
            result_id, product_id, verdict, test_date, issue, test_name,
            findings, source_urls, extraction_model, extraction_confidence, origin
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """,
        result_id,
        product_id,
        verdict,
        test_date,
        issue,
        test_name,
        json.dumps(findings),
        source_urls,
        extraction_model,
        extraction_confidence,
        origin,
    )


async def find_conflicting_result(product_id: str, issue: str | None) -> dict | None:
    if not issue:
        return None
    return await pool().fetchrow(
        "SELECT * FROM test_results WHERE product_id = $1 AND issue = $2 AND is_current = true",
        product_id,
        issue,
    )


async def enqueue_review(*, review_id: str, kind: str, payload: dict) -> None:
    await pool().execute(
        "INSERT INTO review_queue (review_id, kind, payload) VALUES ($1,$2,$3)",
        review_id,
        kind,
        json.dumps(payload),
    )


async def create_scan(
    *,
    scan_id: str,
    api_key_id: str,
    image_sha256: str,
    hint: str | None,
    locale: str,
    webhook_url: str | None,
    allow_live_lookup: bool,
    context: dict | None,
) -> None:
    await pool().execute(
        """
        INSERT INTO scans (
            scan_id, api_key_id, image_sha256, hint, locale, webhook_url,
            allow_live_lookup, context
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """,
        scan_id,
        api_key_id,
        image_sha256,
        hint,
        locale,
        webhook_url,
        allow_live_lookup,
        json.dumps(context) if context else None,
    )


async def mark_processing(scan_id: str) -> None:
    await pool().execute("UPDATE scans SET status = 'processing' WHERE scan_id = $1 AND status = 'queued'", scan_id)


async def get_scan(scan_id: str) -> dict | None:
    return await pool().fetchrow("SELECT * FROM scans WHERE scan_id = $1", scan_id)


async def purge_expired_scans(retention_days: int) -> int:
    """Deletes scan rows past retention. The derived catalog (products,
    test_results) is untouched — only the scan/image-lookup record goes."""
    result = await pool().execute(
        "DELETE FROM scans WHERE created_at < now() - ($1 || ' days')::interval",
        str(retention_days),
    )
    return int(result.split()[-1]) if result else 0


async def find_completed_scan_by_sha256(image_sha256: str) -> dict | None:
    # Only reuse a prior scan that was actually resolved, or the stable
    # product_untested finding (which has its own recheck TTL). Retryable
    # outcomes (live_lookup_disabled, image_unreadable, ...) must NOT be
    # permanently locked to this image hash — the caller is meant to be able
    # to get a different answer on retry (budget reset, better photo, etc.).
    return await pool().fetchrow(
        """
        SELECT * FROM scans
        WHERE image_sha256 = $1 AND status = 'completed'
          AND (resolution_reason IS NULL OR resolution_reason = 'product_untested')
        ORDER BY created_at DESC LIMIT 1
        """,
        image_sha256,
    )


async def update_scan_result(
    *,
    scan_id: str,
    status: str,
    resolution_tier: str | None,
    resolution_reason: str | None,
    product_id: str | None,
    confidence: float | None,
    pipeline: dict,
    warnings: list[dict],
) -> None:
    await pool().execute(
        """
        UPDATE scans SET
            status = $2, resolution_tier = $3, resolution_reason = $4,
            product_id = $5, confidence = $6, pipeline = $7, warnings = $8,
            completed_at = now()
        WHERE scan_id = $1
        """,
        scan_id,
        status,
        resolution_tier,
        resolution_reason,
        product_id,
        confidence,
        json.dumps(pipeline),
        json.dumps(warnings),
    )


async def insert_feedback(*, feedback_id: str, scan_id: str, kind: str, payload: dict) -> None:
    await pool().execute(
        "INSERT INTO feedback (feedback_id, scan_id, kind, payload) VALUES ($1,$2,$3,$4)",
        feedback_id,
        scan_id,
        kind,
        json.dumps(payload),
    )


async def search_products(
    *, q: str | None, embedding: list[float] | None, gtin: str | None, category: str | None, limit: int, min_score: float
) -> list[dict]:
    if gtin:
        row = await find_product_by_gtin(gtin)
        return [dict(row, score=1.0)] if row else []
    if embedding is None:
        return []
    clauses = ["embedding IS NOT NULL"]
    params: list = [to_pgvector(embedding)]
    if category:
        params.append(category)
        clauses.append(f"category LIKE ${len(params)} || '%%'")
    params.append(limit)
    sql = f"""
        SELECT *, 1 - (embedding <=> $1::vector) AS score
        FROM products
        WHERE {' AND '.join(clauses)}
        ORDER BY embedding <=> $1::vector
        LIMIT ${len(params)}
    """
    rows = await pool().fetch(sql, *params)
    return [r for r in rows if r["score"] >= min_score]
