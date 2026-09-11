from fastapi import APIRouter, Depends, Query, Response

from app import repo
from app.auth import ApiKey, require_scope
from app.config import settings
from app.errors import ApiError, not_found
from app.openrouter import ModelCallError, client
from app.ratelimit import check_read_limit
from app.schemas import serialize_product_detail

router = APIRouter(prefix="/v1/products", tags=["products"])


@router.get("/search")
async def search_products(
    response: Response,
    q: str | None = None,
    gtin: str | None = None,
    category: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    min_score: float = Query(0.60, ge=0.0, le=1.0),
    key: ApiKey = Depends(require_scope("read")),
):
    rl = check_read_limit(key.key_id, per_min=settings.read_rate_limit_per_min)
    response.headers.update(rl.headers())

    embedding = None
    match_type = "gtin" if gtin else "vector"
    if not gtin and q:
        try:
            embedding, _ = await client.embed(q, kind="query")
        except ModelCallError as e:
            raise ApiError(502, "upstream_model_error", str(e))

    rows = await repo.search_products(q=q, embedding=embedding, gtin=gtin, category=category, limit=limit, min_score=min_score)
    results = []
    for r in rows:
        current = await repo.get_current_test_results(r["product_id"])
        latest = current[0] if current else None
        results.append(
            {
                "product_id": r["product_id"],
                "brand": r["brand"],
                "name": r["name"],
                "variant": r["variant"],
                "score": float(r["score"]),
                "verdict": latest["verdict"] if latest else None,
                "test_date": latest["test_date"] if latest else None,
            }
        )
    return {"query": q or gtin or "", "match_type": match_type, "results": results, "total": len(results)}


@router.get("/{product_id}")
async def get_product(product_id: str, response: Response, key: ApiKey = Depends(require_scope("read"))):
    rl = check_read_limit(key.key_id, per_min=settings.read_rate_limit_per_min)
    response.headers.update(rl.headers())
    product = await repo.get_product(product_id)
    if not product:
        raise not_found("product_id")
    results = await repo.get_current_test_results(product_id)
    return serialize_product_detail(product, results)
